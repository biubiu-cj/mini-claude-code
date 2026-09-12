import threading
import time
import queue

from agent import agent_loop
from cron_scheduler import has_due_jobs, start_scheduler
from hooks import register_default_hooks, trigger_hooks
from memory import MEMORY_MANAGER
from permissions import set_interactive_allowed
from prompts import build_system_prompt
from team import BUS, consume_lead_inbox, format_team_events
from todo import TodoManager
from util import clean_text

# 用户回合和定时回合靠它串行:同一时刻只跑一轮 agent
TURN_LOCK = threading.Lock()
# 定时回合的占位请求;真正的任务内容由 agent_loop 里的 inject_cron_jobs 注入
SCHEDULED_REQUEST = "[scheduled] Handle the due cron job(s)."


# stdin 读取线程用:队列里放用户输入的每一行,None 表示 EOF
_input_queue: "queue.Queue[str | None]" = queue.Queue()


def stdin_reader():
    """后台线程:把终端输入逐行塞进队列,EOF 时投 None。"""
    while True:
        try:
            line = input("Enter your query (exit to quit): ")
        except EOFError:
            _input_queue.put(None)
            return
        _input_queue.put(line)


def wait_for_cli_event():
    """等用户输入或 Lead 收件箱,返回 (kind, payload)。"""
    while True:
        if BUS.peek("lead"):
            return "wake", None
        try:
            line = _input_queue.get(timeout=0.25)
        except queue.Empty:
            continue
        if line is None:
            return "quit", None
        return "user", line


def start_session(query):
    """构造会话级 messages 和 todo,整个 CLI 会话复用它们。"""
    relevant_memories = MEMORY_MANAGER.load_memories(query)
    trigger_hooks("UserPromptSubmit", query)
    messages = [
        {
            "role": "system",
            "content": build_system_prompt(relevant_memories),
        },
        {
            "role": "user",
            "content": clean_text(query),
        },
    ]
    return messages, TodoManager(), relevant_memories

def run_query(query):
    """跑一轮 agent(用户提问与定时任务共用同一条路径)。"""
    relevant_memories = MEMORY_MANAGER.load_memories(query)
    trigger_hooks("UserPromptSubmit", query)

    messages = [
        {
            "role": "system",
            "content": build_system_prompt(relevant_memories)
        },
        {
            "role": "user",
            "content": clean_text(query)
        }
    ]

    # 每个 Agent 会话启动时创建自己的 TodoManager,显式传给 agent_loop
    parent_todo = TodoManager()
    agent_loop(messages, parent_todo, query, relevant_memories)


def scheduled_worker():
    """队列处理线程:空闲时把到期任务交给 Agent Loop。"""
    while True:
        time.sleep(1)
        if not has_due_jobs():
            continue
        with TURN_LOCK:
            # 用户回合可能刚把任务取走,锁内再确认一次
            if not has_due_jobs():
                continue
            # 定时回合拒绝交互确认,不会与主终端同时读输入
            set_interactive_allowed(False)
            try:
                print("\n[Cron] scheduled turn start")
                run_query(SCHEDULED_REQUEST)
            finally:
                set_interactive_allowed(True)


if __name__ == "__main__":
    # 显式注册内置 hook(不再依赖 import 副作用,避免重复注册)
    register_default_hooks()
    # 调度线程和队列处理线程只在运行 CLI 时启动
    start_scheduler()
    threading.Thread(target=scheduled_worker, daemon=True).start()
    # stdin 从后台线程读,主线程才腾得出来等收件箱
    threading.Thread(target=stdin_reader, daemon=True).start()

    session_messages = None
    session_todo = None
    session_memories = ()
    session_request = ""

    while True:
        kind, payload = wait_for_cli_event()
        if kind == "quit":
            break
        if kind == "user":
            if not payload.strip():
                continue
            if payload.strip().lower() in ("exit", "quit"):
                break
            if session_messages is None:
                # 第一条输入:建会话级上下文
                session_messages, session_todo, session_memories = (
                    start_session(payload)
                )
                session_request = payload
            else:
                # 后续输入:追加到同一个会话
                trigger_hooks("UserPromptSubmit", payload)
                session_messages.append({
                    "role": "user",
                    "content": clean_text(payload),
                })
            is_team_turn = False
        else:
            # 队友事件唤醒:先更新协议状态,再注入
            inbox = consume_lead_inbox()
            if not inbox:
                continue
            if session_messages is None:
                continue
            session_messages.append({
                "role": "user",
                "content": format_team_events(inbox),
            })
            print(f"[wake: {len(inbox)} team event(s) -> new turn]")
            is_team_turn = True

        with TURN_LOCK:
            agent_loop(
                session_messages,
                session_todo,
                session_request,
                session_memories,
                is_team_turn,
            )
        print()