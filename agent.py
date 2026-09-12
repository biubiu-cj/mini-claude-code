from dataclasses import dataclass
import json
import os
import time

from background_task import BACKGROUND, should_run_background
from compaction import COMPACTOR
from config import (
    DEFAULT_MAX_TOKENS,
    ESCALATED_MAX_TOKENS,
    FALLBACK_MODEL,
    MAIN_OWNER,
    MAX_CONSECUTIVE_529,
    MAX_RECOVERY_RETRIES,
    MAX_RETRIES,
    MODEL,
    PLAN_CHECK_EVERY,
    REMINDER_EVERY,
    client,
)
from cron_scheduler import consume_cron_queue, list_crons, save_durable_jobs
from hooks import trigger_hooks
from mcp_client import assemble_tool_pool, run_connect_mcp
from memory import MEMORY_MANAGER
from prompts import build_sub_system_prompt, build_system_prompt
from team import run_create_worktree, run_list_teammates, run_request_plan, run_request_shutdown, run_review_plan, run_send_message, run_spawn_teammate
from todo import TodoManager
from tool_handlers import (
    claim_task,
    complete_task,
    create_task,
    get_task,
    list_tasks,
    run_bash,
    run_cancel_cron,
    run_edit,
    run_glob,
    run_load_skill,
    run_read,
    run_schedule_cron,
    run_todo_write,
    run_write,
    start_background_task,
    update_task,
)
from toolschema import SUB_TOOLS
from util import clean_text, parse_json


# 算作"规划活动"的工具:调用它们就认为模型在维护计划,不该再催它去建计划。
# 任务板也算,否则模型在板子上按依赖推进时会被反复催"你还没建计划"
PLANNING_TOOLS = {
    "todo_write",
    "create_task",
    "update_task",
    "claim_task",
    "complete_task",
}

# 其中属于持久任务板的
BOARD_TOOLS = PLANNING_TOOLS - {"todo_write"}


def inject_background_results(messages):
    """把已完成的后台任务通知以 user 消息注入上下文,返回注入条数。"""
    notifications = BACKGROUND.collect()
    for notification in notifications:
        messages.append({"role": "user", "content": notification})
    if notifications:
        print(f"\n[Background] {len(notifications)} notification(s) injected")
    return len(notifications)


def inject_cron_jobs(messages):
    """把到期的定时任务以 user 消息注入上下文,返回注入条数。"""
    jobs = consume_cron_queue()
    for job in jobs:
        messages.append({
            "role": "user",
            "content": (
                "<cron_notification>\n"
                f"Job ID: {job.id}\n"
                f"Schedule: {job.cron}\n"
                f"Task to perform now: {job.prompt}\n"
                "</cron_notification>"
            ),
        })
    if any(job.durable and not job.recurring for job in jobs):
        # 一次性 durable 任务已从内存删除,交付之后再写回:
        # 若进程在这个窗口内退出,重启后同一任务会再次交付(至少一次),
        # 顺序不能反过来,先写回再交付会丢任务
        save_durable_jobs()
    if jobs:
        print(f"\n[Cron] {len(jobs)} job(s) injected")
    return len(jobs)


def refresh_system_prompt(messages, build, relevant_memories, todo_text):
    """把当前计划写回 system 消息。

    计划若只作为一次 tool result 存在,会被上下文压缩丢掉;而每条压缩路径
    (snip / micro / fit / compact_history / reactive_compact)都保留 system 消息,
    所以放在这里最稳,PLAN_CHECK_EVERY 的自检提醒也才有东西可对照。
    """
    system = next(
        (message for message in messages if message.get("role") == "system"), None
    )
    if system is not None:
        system["content"] = build(relevant_memories, todo_text)


@dataclass
class RecoveryState:
    """一次 agent 轮次内的恢复状态:max_tokens 升级、529 计数、继续写次数。"""
    has_escalated: bool = False
    consecutive_529: int = 0
    recovery_count: int = 0


def retry_delay(attempt):
    """指数退避的等待秒数。"""
    return min(2 ** attempt, 30)


def call_model(messages, tools, state, max_tokens):
    """带恢复的模型调用:429/529 退避重试,必要时切备用模型。"""
    model = MODEL
    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=model, messages=messages, tools=tools,
                tool_choice="auto", max_tokens=max_tokens,
            )
            state.consecutive_529 = 0
            return response
        except Exception as error:
            text = str(error).lower()
            if "429" in text or "rate limit" in text or "ratelimit" in text:
                kind = "429"
            elif "529" in text or "overloaded" in text:
                kind = "529"
                state.consecutive_529 += 1
                if state.consecutive_529 >= MAX_CONSECUTIVE_529 and FALLBACK_MODEL:
                    model = FALLBACK_MODEL
                    state.consecutive_529 = 0
                    print(f"\n[529] switching to {FALLBACK_MODEL}")
            else:
                raise
            # 重试耗尽就抛出**原始**错误:套一层笼统的 RuntimeError 会把
            # 429 / 529 的区分盖掉,日志里就看不出真实原因
            if attempt == MAX_RETRIES - 1:
                raise
            delay = retry_delay(attempt)
            print(f"\n[{kind}] retry {attempt + 1}/{MAX_RETRIES} in {delay:.0f}s")
            time.sleep(delay)
    # 只有 MAX_RETRIES <= 0 才会走到这里
    raise RuntimeError(f"model call failed after {MAX_RETRIES} attempts")





#agent loop
def agent_loop(messages, todo_manager, active_request, relevant_memories=(),
               is_team_turn=False):
    """主 Agent 循环:一路跑到模型不再调用工具为止。"""
    tool_calls_since_todo = 0  # 连续未更新 todo 的工具调用数
    board_used = False         # 本会话是否动过持久任务板
    pending_checkin = False  # 刚注入自检提醒:下一轮纯文本是应答,不是任务完成
    bg_stop_nudged = False  # 退出前已提醒过"后台任务还在跑"
    reactive_retries = 0 #补救次数
    state = RecoveryState() #恢复状态
    max_tokens = DEFAULT_MAX_TOKENS
    while True:
        # 每轮先把当前计划刷进 system 消息,让它扛得住压缩
        refresh_system_prompt(
            messages, build_system_prompt, relevant_memories, todo_manager.render()
        )
        messages = COMPACTOR.prepare(messages, active_request)
        # 压缩之后再注入:通知作为最新消息,不会被本轮的压缩吞掉
        inject_background_results(messages)
        inject_cron_jobs(messages)
        try:
            tools, handlers = assemble_tool_pool(TOOL_HANDLERS)
        except ValueError as error:
            # 工具池组装失败(撞名/超长/非法 schema)是配置问题:
            # 报出来并结束本轮,不要让整个 CLI 崩掉
            print(f"\n[MCP] tool pool error: {error}")
            messages.append({
                "role": "assistant",
                "content": f"[MCP tool pool error] {error}",
            })
            return
        try:
            response = call_model(messages, tools, state, max_tokens)
            # 请求成功，重置上下文过长的补救次数
            reactive_retries = 0
        except Exception as error:
            error_message = str(error).lower()
            too_long = (
                "prompt_too_long" in error_message
                or "too many tokens" in error_message
                or "context length" in error_message
            )
            if too_long and reactive_retries < 1:
                print("[context too long, reactive compact]")
                messages = COMPACTOR.reactive_compact(messages, active_request)
                reactive_retries += 1
                # 压缩之后重新调用模型
                continue
            raise
        # 模型生成达到 max_tokens 上限
        if response.choices[0].finish_reason == "length":
            if not state.has_escalated:
                # 第一次:丢掉这段被截断的输出,用更大的预算重跑
                state.has_escalated = True
                max_tokens = ESCALATED_MAX_TOKENS
                print(f"\n[max_tokens] escalating to {max_tokens}")
                continue
            # 已经升过级还顶到上限:必须先把这段被截断的输出留下。
            # 否则模型收到"接着写",上下文里却没有它自己写过什么,无从继续。
            messages.append({
                "role": "assistant",
                "content": response.choices[0].message.content or "",
            })
            if state.recovery_count >= MAX_RECOVERY_RETRIES:
                # 兜底上限:没有它,模型只要一直在写长输出就会无限 continue
                print(f"\n[max_tokens] gave up after "
                      f"{MAX_RECOVERY_RETRIES} continuations")
                return
            state.recovery_count += 1
            messages.append({
                "role": "user",
                "content": "Continue exactly where you left off.",
            })
            continue
        # 正常响应:复位,下次再溢出还能重新升级
        max_tokens = DEFAULT_MAX_TOKENS
        state.has_escalated = False
        message = response.choices[0].message
        tool_calls = message.tool_calls

        assistant_message = {"role": "assistant", "content": message.content or ""}
        if tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": tool_call.id, "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in tool_calls
            ]
        #追加模型提示词
        messages.append(assistant_message)

        if not tool_calls:
            if pending_checkin:
                # 纯文本 = 对自检提醒的应答(如"继续执行"),不是任务完成
                pending_checkin = False
                if message.content:
                    print(message.content)
                messages.append({
                    "role": "user",
                    "content": "Continue the task now: call tools directly, "
                               "and give the final answer only when "
                               "everything is done.",
                })
                continue
            # 模型想收尾:先补投已完成的后台结果;还有在跑的提醒一次,
            # 让它自己决定是等结果还是放弃(再答纯文本就放行,不再拦)
            if inject_background_results(messages):
                if message.content:
                    print(message.content)
                continue
            running = BACKGROUND.running()
            if running and not bg_stop_nudged:
                bg_stop_nudged = True
                if message.content:
                    print(message.content)
                messages.append({
                    "role": "user",
                    "content": (
                        "[Reminder] Background task(s) still running:\n"
                        + "\n".join(running)
                        + "\nIf you need their results, wait for them first "
                          "(e.g. run a short sleep command; a finished task's "
                          "result is injected automatically). If you do not "
                          "need them, give your final answer now — tasks "
                          "still running when the session ends are killed."
                    ),
                })
                continue
            if message.content:
                print(message.content)
            force = trigger_hooks("Stop", messages)   # ← 退出之前
            if force:
                # hook returned a message → inject it and continue
                messages.append({"role": "user", "content": force})
                continue
            if not is_team_turn:
                MEMORY_MANAGER.extract_memories(messages)
                MEMORY_MANAGER.consolidate_memories()
            return

        had_todo_in_round = False
        # 出现工具调用即视为已应答自检(或正在正常工作)
        pending_checkin = False
        # 模型又在干活了(sleep 等结果也算),下次退出时允许再提醒一次
        bg_stop_nudged = False

        compact_requested = False
        for tool_call in tool_calls:
            # compact 是循环级动作,不走 handler:先回占位结果保住配对,
            # 等整轮结果都追加完再压缩(否则会丢掉这条 assistant 消息,API 报 400)
            if tool_call.function.name == "compact":
                compact_requested = True
                messages.append({
                    "role": "tool", "tool_call_id": tool_call.id,
                    "content": "[Compaction requested. This completed turn "
                               "will be summarized.]",
                })
                continue
            try:
                arguments = parse_json(tool_call.function.arguments)
            except (json.JSONDecodeError, TypeError) as error:
                # 坏 JSON 不该掀翻整个 loop,作为工具结果还给模型让它自己纠正
                messages.append({
                    "role": "tool", "tool_call_id": tool_call.id,
                    "content": f"Error: invalid tool arguments JSON: {error}",
                })
                continue
            #权限校验
            permission_result = trigger_hooks("PreToolUse", tool_call, arguments)

            if permission_result is not None:
                print(f"\n[Tool] {tool_call.function.name} denied: {permission_result}")
                messages.append({
                    "role": "tool", "tool_call_id": tool_call.id,
                    "content": f"Permission denied: {permission_result}",
                })
                continue

            tool_name = tool_call.function.name
            # run_in_background 是给 loop 的标志,消费后不传给 handler
            background_call = should_run_background(tool_name, arguments)
            arguments.pop("run_in_background", None)
            handler = handlers.get(tool_name)
            if handler is None:
                messages.append({
                    "role": "tool", "tool_call_id": tool_call.id,
                    "content": f"Error: unknown tool {tool_name}",
                })
                continue
            # TodoManager 是 Agent 运行时内部状态,不由 LLM 传入;
            # 只对 todo_write 注入当前 Agent 的实例,其余工具签名不变
            try:
                if background_call:
                    output = start_background_task(arguments["command"])
                elif tool_name == "todo_write":
                    output = clean_text(handler(**arguments, todo_manager=todo_manager))
                elif tool_name == "task":
                    # 子 agent 复用父循环已召回的 memory,注入方式同 todo_manager
                    output = clean_text(
                        handler(**arguments, relevant_memories=relevant_memories)
                    )
                elif tool_name in ("claim_task", "complete_task"):
                    # owner 由运行时注入:模型自己填的身份不构成任何约束
                    output = clean_text(handler(**arguments, owner=MAIN_OWNER))
                else:
                    output = clean_text(handler(**arguments))
            except Exception as e:
                # 工具出错不崩:把错误文本作为工具结果还给模型,让它自己纠正
                output = f"Error: {type(e).__name__}: {e}"
            if tool_name in PLANNING_TOOLS:
                had_todo_in_round = True
                tool_calls_since_todo = 0
            else:
                tool_calls_since_todo += 1
            if tool_name in BOARD_TOOLS:
                board_used = True
            trigger_hooks("PostToolUse", tool_call, arguments, output)
            messages.append({
                "role": "tool", "tool_call_id": tool_call.id, "content": output,
            })

        if compact_requested:
            # 必须切片赋值就地替换:agent_loop 的 messages 每轮会被
            # COMPACTOR.prepare 重新绑定,直接赋值只改局部名字,调用方看不到
            messages[:] = COMPACTOR.compact_history(messages, active_request)

        # 一轮结束:用独立 user 消息提醒(以独立消息推送比拼在工具 output
        # 末尾可靠得多)。分两种场景:
        # 1) 尚无任何计划 → 提醒先建计划(频率密:REMINDER_EVERY)
        # 2) 已有计划但长期未自检/更新 → 提醒对照计划自检是否偏离(频率疏:PLAN_CHECK_EVERY)
        reminder = None
        if (not had_todo_in_round and not todo_manager.items
                and not board_used
                and tool_calls_since_todo >= REMINDER_EVERY):
            reminder = (
                "[Reminder] You have made several tool calls without creating "
                "a task plan. If this task has multiple steps, call todo_write "
                "NOW to create the plan, then continue with the work."
            )
            print("\n[Reminder] asking the model to create a todo plan")
        elif (not had_todo_in_round
                and (todo_manager.items or board_used)
                and tool_calls_since_todo >= PLAN_CHECK_EVERY):
            reminder = (
                "[Reminder] You have made many tool calls without checking "
                "your task plan. Review it now: 1) if your current work "
                "deviates from the plan or a step just finished, call "
                "todo_write to update it; 2) if everything is on plan, "
                "reply \"continue\" and do not call any tool."
            )
            print("\n[Reminder] asking the model to self-check against the plan")
        if reminder:
            tool_calls_since_todo = 0
            pending_checkin = True
            messages.append({"role": "user", "content": reminder})


def run_subagent(prompt: str, relevant_memories=()) -> str:
    """跑一个一次性子 Agent,只有它返回的最终文本会回到父上下文。"""
    # 每个 Subagent 会话创建自己的 TodoManager,与父 Agent / 其它 Subagent 完全独立
    child_todo = TodoManager()
    # 独立的归属身份:任务图里的 owner 就是靠它区分的,
    # 因此子 agent 只能完成自己认领的任务,父 agent 也不能替它完成
    child_owner = "sub_" + os.urandom(3).hex()
    messages = [
        {"role": "system", "content": build_sub_system_prompt(relevant_memories)},
        {"role": "user", "content": clean_text(prompt)},
    ]
    reactive_retries = 0 #补救次数
    tool_calls_since_todo = 0  # 连续未更新 todo 的工具调用数(每个子会话独立)
    pending_checkin = False  # 刚注入自检提醒:下一轮纯文本是应答,不是任务完成
    for _ in range(30):
        # 同父循环:把子会话自己的计划刷进 system 消息
        refresh_system_prompt(
            messages, build_sub_system_prompt, relevant_memories, child_todo.render()
        )
        messages = COMPACTOR.prepare(messages, prompt)
        try:
            response = client.chat.completions.create(
                model=MODEL, messages=messages, tools=SUB_TOOLS, tool_choice="auto",
            )
            reactive_retries = 0
        except Exception as error:
            error_message = str(error).lower()
            too_long = (
                "prompt_too_long" in error_message
                or "too many tokens" in error_message
                or "context length" in error_message
            )

            if (too_long and reactive_retries < 1):
                print("[subagent context too long, reactive compact]")
                messages = COMPACTOR.reactive_compact(messages, prompt)
                reactive_retries += 1
                continue
            raise

        message = response.choices[0].message
        tool_calls = message.tool_calls
        assistant_message = {"role": "assistant", "content": message.content or ""}
        if tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": tool_call.id, "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in tool_calls
            ]
        messages.append(assistant_message)
        if not tool_calls:
            if pending_checkin:
                # 纯文本 = 对自检提醒的应答,不是子任务完成
                pending_checkin = False
                messages.append({
                    "role": "user",
                    "content": "Continue the task now: call tools directly, "
                               "and give the final answer only when "
                               "everything is done.",
                })
                continue
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return message.content or ""
        had_todo_in_round = False
        # 出现工具调用即视为已应答自检(或正在正常工作)
        pending_checkin = False
        for tool_call in tool_calls:
            try:
                arguments = parse_json(tool_call.function.arguments)
            except (json.JSONDecodeError, TypeError) as error:
                messages.append({
                    "role": "tool", "tool_call_id": tool_call.id,
                    "content": f"Error: invalid tool arguments JSON: {error}",
                })
                continue
            permission_result = trigger_hooks("PreToolUse", tool_call, arguments)
            if permission_result is not None:
                output = f"Permission denied: {permission_result}"
            else:
                tool_name = tool_call.function.name
                # run_in_background 是给 loop 的标志,消费后不传给 handler;
                # 子 agent 启动的后台任务,完成通知统一注入父对话
                background_call = should_run_background(tool_name, arguments)
                arguments.pop("run_in_background", None)
                # 只用子 agent 白名单(由 SUB_TOOLS 推导),防止模型发出 task
                # 造成无限递归
                handler = SUB_HANDLERS[tool_name]
                # 同 agent_loop:todo_write 注入本子会话的 TodoManager;
                # claim/complete 注入本子会话的 owner
                try:
                    if background_call:
                        output = start_background_task(arguments["command"])
                    elif tool_name == "todo_write":
                        output = clean_text(handler(**arguments, todo_manager=child_todo))
                    elif tool_name in ("claim_task", "complete_task"):
                        output = clean_text(handler(**arguments, owner=child_owner))
                    else:
                        output = clean_text(handler(**arguments))
                except Exception as e:
                    # 工具出错不崩:把错误文本作为工具结果还给模型,让它自己纠正
                    output = f"Error: {type(e).__name__}: {e}"
                if tool_name in PLANNING_TOOLS:
                    had_todo_in_round = True
                    tool_calls_since_todo = 0
                else:
                    tool_calls_since_todo += 1
                trigger_hooks("PostToolUse", tool_call, arguments, output)
            messages.append({
                "role": "tool", "tool_call_id": tool_call.id, "content": output,
            })

        # 一轮结束:连续多次未用 todo 更新计划 → 用独立 user 消息提醒
        # (与父 loop 相同,分"无计划建计划"和"有计划自检偏离"两种场景)
        reminder = None
        if (not had_todo_in_round and not child_todo.items
                and tool_calls_since_todo >= REMINDER_EVERY):
            reminder = (
                "[Reminder] You have made several tool calls without creating "
                "a task plan. If this task has multiple steps, call todo_write "
                "NOW to create the plan, then continue with the work."
            )
            print("\n[Reminder] asking the model to create a todo plan")
        elif (not had_todo_in_round and child_todo.items
                and tool_calls_since_todo >= PLAN_CHECK_EVERY):
            reminder = (
                "[Reminder] You have made many tool calls without checking "
                "your task plan. Review it now: 1) if your current work "
                "deviates from the plan or a step just finished, call "
                "todo_write to update it; 2) if everything is on plan, "
                "reply \"continue\" and do not call any tool."
            )
            print("\n[Reminder] asking the model to self-check against the plan")
        if reminder:
            tool_calls_since_todo = 0
            pending_checkin = True
            messages.append({"role": "user", "content": reminder})
    return "Subagent stopped after 30 turns without a final answer."


#工具映射表
TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "todo_write": run_todo_write,
    "task": run_subagent,
    "load_skill": run_load_skill,

    "create_task": create_task,
    "update_task": update_task,
    "list_tasks": list_tasks,
    "get_task": get_task,
    "claim_task": claim_task,
    "complete_task": complete_task,

    "spawn_teammate": run_spawn_teammate,
    "list_teammates": run_list_teammates,
    "send_message": run_send_message,
    "request_shutdown": run_request_shutdown,
    "request_plan": run_request_plan,
    "review_plan": run_review_plan,
    "create_worktree": run_create_worktree,

    "connect_mcp": run_connect_mcp,
    "schedule_cron": run_schedule_cron,
    "list_crons": list_crons,
    "cancel_cron": run_cancel_cron,
}

# 子 agent 的白名单直接由 SUB_TOOLS 推导:工具列表和 handler 表不会各改一处而错位。
# 它自动排除了 task(防递归委派),但保留持久任务板用于多 agent 协作
SUB_HANDLERS = {
    tool["function"]["name"]: TOOL_HANDLERS[tool["function"]["name"]]
    for tool in SUB_TOOLS
}
