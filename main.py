import argparse
import threading
import time
import queue

from agent import agent_loop
from background_task import BACKGROUND
from cron_scheduler import consume_cron_queue, has_due_jobs, save_durable_jobs, start_scheduler
from goal import GOAL, parse_goal_input
from hooks import register_default_hooks, trigger_hooks
from memory import MEMORY_MANAGER
from permissions import set_interactive_allowed
from prompts import build_system_prompt
from session import SessionStore, latest_session_id, new_session_id, valid_session_id
from taskboard import reap_orphaned_tasks
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
    """等用户输入 / 队友消息 / 后台任务完成 / 定时任务。"""
    while True:
        # 1. 队友消息，优先级最高
        if BUS.peek("lead"):
            return "wake", None

        # 2. 后台任务完成
        if BACKGROUND.has_ready():
            return "background", None

        # 3. Cron 到期
        if has_due_jobs():
            return "cron", None

        # 4. 用户输入
        try:
            line = _input_queue.get(timeout=0.25)
        except queue.Empty:
            continue

        if line is None:
            return "quit", None

        return "user", line


def parse_args(argv=None):
    """解析命令行参数:--continue 续最近一次会话,--resume <id> 续指定会话。"""
    parser = argparse.ArgumentParser(prog="main.py", description="mini Claude Code")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--continue", dest="continue_latest", action="store_true",
        help="接着最近一次会话跑",
    )
    group.add_argument(
        "--resume", dest="resume_id", metavar="SESSION_ID",
        help="续跑指定会话 id,形如 20260925-204501",
    )
    return parser.parse_args(argv)


def restore_session(store):
    """从存储里还原一次会话;没有消息可还原时返回 None。"""
    messages = store.load_messages()
    if not messages:
        return None
    meta = store.load_meta()
    request = meta["active_request"]
    # 计划必须一起恢复:它每轮都会被写回 system 消息,PLAN_CHECK_EVERY 的
    # 自检提醒也拿它当对照;丢了计划等于把"已建计划"的状态清零
    todo = TodoManager()
    todo.items = meta["todo"]
    # 恢复出来的历史可能已经超长,但这里不用提前压缩:
    # agent_loop 每轮开头都会跑 COMPACTOR.prepare
    return messages, todo, MEMORY_MANAGER.load_memories(request), request


def persist_session(store, messages, todo, active_request):
    """一轮结束后把新增消息与当前计划落盘。"""
    added = store.sync(messages)
    store.save_meta(active_request, todo.items)
    if added:
        print(f"\033[90m[session] +{added} message(s) -> {store.path.name}\033[0m")


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


def run_cron_turn(messages, todo, active_request, memories):
    """在主线程里把到期任务注入同一个会话,然后跑一轮。"""
    jobs = consume_cron_queue()
    if not jobs:
        return
    for job in jobs:
        messages.append({
            "role": "user",
            "content": f"[Scheduled] {job.prompt}",
        })
        print(f"\n[Cron] injected: {job.prompt[:60]}")
    if any(job.durable and not job.recurring for job in jobs):
        # 一次性 durable 任务已从内存删除,交付之后再写回
        save_durable_jobs()
    # 定时回合不应该弹交互确认
    set_interactive_allowed(False)
    try:
        agent_loop(
            messages,
            todo,
            active_request,
            memories,
            True,
        )
    finally:
        set_interactive_allowed(True)


if __name__ == "__main__":
    # 显式注册内置 hook(不再依赖 import 副作用,避免重复注册)
    register_default_hooks()
    # 上个进程崩溃/被杀时,持有任务的队友线程和子 agent 都没机会回收自己认领的
    # 任务。此刻还没有任何线程活着,所以凡是 in_progress 都是孤儿 —— 不退回
    # pending 的话它们会永远卡死:原 owner 不会再出现,别人也认领不了
    orphaned = reap_orphaned_tasks()
    if orphaned:
        print(f"[recover] released {len(orphaned)} orphaned task(s) back to pending: "
              + ", ".join(orphaned))
    # 调度线程和队列处理线程只在运行 CLI 时启动
    start_scheduler()
    # stdin 从后台线程读,主线程才腾得出来等收件箱
    threading.Thread(target=stdin_reader, daemon=True).start()

    args = parse_args()
    if args.resume_id and not valid_session_id(args.resume_id):
        print(f"[session] invalid session id: {args.resume_id}")
        raise SystemExit(2)
    if args.resume_id:
        session_id = args.resume_id
    elif args.continue_latest:
        session_id = latest_session_id()
        if session_id is None:
            print("[session] no saved session to continue, starting a new one")
            session_id = new_session_id()
    else:
        session_id = new_session_id()
    store = SessionStore(session_id)

    session_messages = None
    session_todo = None
    session_memories = ()
    session_request = ""

    if args.resume_id or args.continue_latest:
        restored = restore_session(store)
        if restored is None:
            print(f"[session] nothing to restore from {session_id}")
        else:
            (session_messages, session_todo,
             session_memories, session_request) = restored
            print(f"\033[90m[session] resumed {session_id}: "
                  f"{len(session_messages)} message(s)\033[0m")

    while True:
        kind, payload = wait_for_cli_event()
        if kind == "quit":
            break
        elif kind == "user":
            if not payload.strip():
                continue
            if payload.strip().lower() in ("exit", "quit"):
                break
            # /goal 是会话级命令:状态查询和 clear 只回话,不产生新任务
            task_text, reply = parse_goal_input(payload)
            if reply is not None:
                print(reply)
            if task_text is None:
                continue
            # 用户重新提问算新的一轮工作:连续 block 计数清零。
            # 后台/定时/队友唤醒不清零 —— 否则上限形同虚设
            GOAL.begin_query()
            if session_messages is None:
                session_messages, session_todo, session_memories = (
                    start_session(task_text)
                )
                session_request = task_text
            else:
                trigger_hooks("UserPromptSubmit", task_text)
                session_messages.append({
                    "role": "user",
                    "content": clean_text(task_text),
                })
            is_team_turn = False

        elif kind == "wake":
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

        elif kind == "background":
            if session_messages is None:
                continue
            print("[wake: background task finished -> new turn]")
            with TURN_LOCK:
                agent_loop(
                    session_messages,
                    session_todo,
                    session_request,
                    session_memories,
                    True,
                )
            persist_session(store, session_messages, session_todo, session_request)
            continue

        elif kind == "cron":
            if session_messages is None:
                continue
            print("[cron: due job -> same session]")
            with TURN_LOCK:
                run_cron_turn(
                    session_messages,
                    session_todo,
                    session_request,
                    session_memories,
                )
            persist_session(store, session_messages, session_todo, session_request)
            continue

        with TURN_LOCK:
            agent_loop(
                session_messages,
                session_todo,
                session_request,
                session_memories,
                is_team_turn,
            )
        persist_session(store, session_messages, session_todo, session_request)

        print()