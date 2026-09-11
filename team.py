# owned by cj
"""s13 Agent Teams:常驻队友、文件收件箱、共享任务认领与协作协议。"""
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from config import BASE_DIR, MODEL, WORKDIR, client
from hooks import trigger_hooks
from permissions import set_interactive_allowed
from taskboard import TASKS, task_store_lock
from tool_handlers import (
    claim_task,
    complete_task,
    list_tasks,
    run_bash,
    run_edit,
    run_glob,
    run_read,
    run_write,
)
from toolschema import TEAMMATE_TOOLS
from util import clean_text, parse_json


# -- 常量 --
MAILBOX_DIR = BASE_DIR / ".mailboxes"
VALID_AGENT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RESERVED_TEAMMATE_NAMES = {"lead", "agent"}

# IDLE 队友等待收件箱的时长:到点没消息就去扫一次任务板
IDLE_SCAN_INTERVAL = 2.0


# -- 全局状态:全部用 team_lock 保护 --
# owner -> {"task_id": str, "cwd": Path}。一个队友同一时刻只持有一个 assignment
teammate_assignments: dict[str, dict] = {}

# owner -> 版本号。认领/释放任务就递增,用来让旧的计划审批失效
assignment_versions: dict[str, int] = {}

# name -> working | waiting_approval | idle | stopping 队友agent的运行状态
active_teammates: dict[str, str] = {}

# name -> not_required | required | pending | approved | rejected #任务审批状态
plan_gates: dict[str, str] = {}

# name -> 当前待审计划的 request_id
plan_request_ids: dict[str, str] = {}

# name -> 队友线程
teammate_threads: dict[str, threading.Thread] = {}

team_lock = threading.RLock()


class MessageBus:
    """基于文件的收件箱:一次只被一个消费者读走,读即删除。"""

    def __init__(self):
        """建一把可重入锁和一个挂在它上面的条件变量。"""
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)

    def _path(self, agent):
        """取收件箱文件路径,顺便挡住路径穿越。"""
        if not VALID_AGENT_NAME.fullmatch(agent or ""):
            raise ValueError(f"Invalid mailbox recipient: {agent!r}")
        path = (MAILBOX_DIR / f"{agent}.jsonl").resolve()
        if not path.is_relative_to(MAILBOX_DIR.resolve()):
            raise ValueError(f"Mailbox path escapes directory: {agent!r}")
        return path

    def _read_unlocked(self, agent):
        """调用方必须已持锁:读完整个收件箱并删掉文件。"""
        inbox = self._path(agent)
        if not inbox.exists():
            return []
        messages = [
            json.loads(line)
            for line in inbox.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        inbox.unlink()
        return messages

    def send(self, from_agent, to_agent, content, msg_type="message", metadata=None):
        """投递一条消息,并唤醒正在等收件箱的队友。"""
        message = {
            "from": from_agent,
            "to": to_agent,
            "content": content,
            "type": msg_type,
            "ts": time.time(),
            "metadata": metadata or {},
        }
        with self._changed:
            MAILBOX_DIR.mkdir(parents=True, exist_ok=True)
            with self._path(to_agent).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(message, ensure_ascii=True) + "\n")
            self._changed.notify_all()
        print(f"  [bus] {from_agent} -> {to_agent}: ({msg_type}) {content[:50]}")

    def read_inbox(self, agent):
        """消费并返回收件箱里的全部消息。"""
        with self._lock:
            return self._read_unlocked(agent)

    def peek(self, agent):
        """收件箱里有没有消息(不移除)。"""
        with self._lock:
            inbox = self._path(agent)
            return inbox.exists() and inbox.stat().st_size > 0

    def wait_for_messages(self, agent, timeout=None):
        """阻塞等到收件箱有消息或超时;超时返回空列表。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while not self.peek(agent):
                remaining = (
                    None if deadline is None else deadline - time.monotonic()
                )
                if remaining is not None and remaining <= 0:
                    return []
                self._changed.wait(remaining)
            return self._read_unlocked(agent)

BUS = MessageBus()




@dataclass
class ProtocolState:
    """一条待处理的控制请求(关机或计划审批)。"""
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str
    work_version: int | None = None
    task_id: str | None = None
    created_at: float = field(default_factory=time.time)

pending_requests: dict[str, ProtocolState] = {}

def new_request_id():
    """生成一个当前未占用的请求 ID。"""
    while True:
        request_id = f"req_{int(time.time() * 1000) % 1000000:06d}"
        with team_lock:
            if request_id not in pending_requests:
                return request_id


def match_response(response_type, request_id, approve, from_agent, to_agent):
    """把一条协议应答关联回它的原始请求,类型/身份/状态三重校验。"""
    with team_lock:
        state = pending_requests.get(request_id)
        if not state:
            print(f"  [protocol] unknown request_id: {request_id}")
            return False
        expected = {
            "shutdown": "shutdown_response",
            "plan_approval": "plan_approval_response",
        }[state.type]
        if response_type != expected:
            print(f"  [protocol] expected {expected}, got {response_type}")
            return False
        if from_agent != state.target or to_agent != state.sender:
            print(f"  [protocol] {request_id} responder mismatch")
            return False
        if state.status != "pending":
            print(f"  [protocol] {request_id} already {state.status}")
            return False
        state.status = "approved" if approve else "rejected"
    print(f"  [protocol] {request_id} -> {state.status}")
    return True


def consume_lead_inbox():
    """消费 Lead 收件箱,先更新协议状态,再把消息交给模型。"""
    messages = BUS.read_inbox("lead")
    for message in messages:
        metadata = message.get("metadata", {})
        request_id = metadata.get("request_id", "")
        if request_id and message.get("type", "").endswith("_response"):
            match_response(
                message["type"],
                request_id,
                metadata.get("approve", False),
                message.get("from", ""),
                message.get("to", ""),
            )
    return messages


def format_team_events(messages):
    """把 Lead 收件箱的消息拼成一段注入 history 的文本。"""
    lines = []
    for message in messages:
        request_id = message.get("metadata", {}).get("request_id")
        suffix = f" request_id={request_id}" if request_id else ""
        lines.append(
            f"[{message['type']}{suffix}] {message['from']}: {message['content']}"
        )
    return "[Team events]\n" + "\n".join(lines)


#------------------------------------------------------


def owner_in_progress(owner):
    """返回 owner 名下进行中的任务,没有则 None。"""
    for task in TASKS.list():
        if task.status == "in_progress" and task.owner == owner:
            return task
    return None


def advance_assignment_version(owner):
    """递增 owner 的工作版本,让基于旧任务签发的计划审批失效。"""
    with team_lock:
        assignment_versions[owner] = assignment_versions.get(owner, 0) + 1
        if owner in plan_gates and plan_gates[owner] != "not_required":
            plan_gates[owner] = "required"
        plan_request_ids.pop(owner, None)


def task_worktree_cwd(task):
    """解析任务的工作目录;绑定不可用时返回错误而不是悄悄回退。"""
    if not task.worktree:
        return WORKDIR, None
    # 阶段 6 会把这里换成 git 校验版本
    path = (BASE_DIR / ".worktrees" / task.worktree).resolve()
    if not path.is_dir():
        return None, f"worktree '{task.worktree}' not found"
    return path, None


def assignment_cwd(owner):
    """返回 owner 当前认领任务的工作目录;没有认领就报错。"""
    with task_store_lock():
        assignment = teammate_assignments.get(owner)
        task = owner_in_progress(owner)
        if task and (not assignment or assignment.get("task_id") != task.id):
            cwd, error = task_worktree_cwd(task)
            if error:
                raise ValueError(error)
            assignment = {"task_id": task.id, "cwd": cwd}
            teammate_assignments[owner] = assignment
        elif not assignment:
            return WORKDIR
        task = TASKS.load(str(assignment["task_id"]))
        if task.status not in {"in_progress", "completed"} or task.owner != owner:
            raise ValueError(f"Assignment for {owner} is no longer active")
        cwd, error = task_worktree_cwd(task)
        if error:
            raise ValueError(error)
        if cwd.resolve() != Path(assignment["cwd"]).resolve():
            raise ValueError(f"Assignment cwd changed for task {task.id}")
        return cwd


def release_completed_assignment(owner):
    """在模型轮次边界释放已完成的 cwd 租约;释放了就返回 True。"""
    with team_lock:
        assignment = teammate_assignments.get(owner)
        if not assignment:
            return False
        task = TASKS.load(str(assignment["task_id"]))
        if task.status != "completed" or task.owner != owner:
            return False
        teammate_assignments.pop(owner, None)
        advance_assignment_version(owner)
        if owner in plan_gates:
            plan_gates[owner] = "not_required"
        return True


def release_teammate_assignment(owner):
    """队友线程退出时,把它没做完的任务退回任务板。"""
    with task_store_lock():
        task = owner_in_progress(owner)
        if task:
            task.status = "pending"
            task.owner = None
            TASKS.save(task)
            print(f"  [teammate] {owner} released {task.id} back to pending")
    with team_lock:
        teammate_assignments.pop(owner, None)
        advance_assignment_version(owner)
        if owner in plan_gates:
            plan_gates[owner] = "not_required"


#———————————————————————————————————————————————————————-
def scan_unclaimed_tasks():
    """列出此刻可认领的候选任务(只是快照,认领时必须重新校验)。"""
    ready = []
    for task in TASKS.list():
        if task.status != "pending" or task.owner is not None:
            continue
        if TASKS.incomplete_dependencies(task):
            continue
        _, error = task_worktree_cwd(task)
        if not error:
            ready.append(task)
    return ready


def team_claim_task(task_id, owner):
    """在锁内认领任务并绑定 owner 的工作目录。"""
    with task_store_lock():
        if teammate_assignments.get(owner):
            return (
                f"Owner {owner} must finish the current work turn for "
                f"{teammate_assignments[owner]['task_id']} "
                "before claiming another task"
            )
        current = owner_in_progress(owner)
        if current:
            return f"Owner {owner} must complete {current.id} before claiming another task"
        task = TASKS.load(task_id)
        cwd, error = task_worktree_cwd(task)
        if error:
            return f"Cannot claim {task_id}: {error}"
        result = claim_task(task_id, owner)
        if not result.startswith("Claimed "):
            return result
        teammate_assignments[owner] = {"task_id": task.id, "cwd": cwd}
        advance_assignment_version(owner)
    return result


def claim_next_task(name):
    """认领第一个还能拿到的任务;已经手上有活就返回 None。"""
    with team_lock:
        if teammate_assignments.get(name) or owner_in_progress(name):
            return None
    for task in scan_unclaimed_tasks():
        result = team_claim_task(task.id, name)
        if result.startswith("Claimed "):
            return TASKS.load(task.id)
    return None


#——————————————————————————————————————————————————————————————————-
def make_tool_call(name):
    """造一个只带 function.name 的伪 tool_call,复用现有 hook 签名。"""
    return SimpleNamespace(function=SimpleNamespace(name=name))


def teammate_submit_plan(from_name, plan):
    """队友提交计划,等待 Lead 审批。"""
    with team_lock:
        assignment = teammate_assignments.get(from_name)
        task_id = str(assignment["task_id"]) if assignment else None
        work_version = assignment_versions.get(from_name, 0)
        if plan_gates.get(from_name) == "pending":
            return "A plan is already waiting for review."
        request_id = new_request_id()
        pending_requests[request_id] = ProtocolState(
            request_id=request_id,
            type="plan_approval",
            sender=from_name,
            target="lead",
            status="pending",
            payload=plan,
            work_version=work_version,
            task_id=task_id,
        )
        plan_gates[from_name] = "pending"
        plan_request_ids[from_name] = request_id
        active_teammates[from_name] = "waiting_approval"
    BUS.send(from_name, "lead", plan, "plan_approval_request",
             {"request_id": request_id})
    return f"Plan submitted ({request_id}). Wait for Lead's decision."


def teammate_send_message(from_name, to, content):
    """队友给 Lead 或其他活跃队友发一条普通消息。"""
    with team_lock:
        if to != "lead" and to not in active_teammates:
            return f"Agent '{to}' is not active"
    BUS.send(from_name, to, content)
    return f"Sent to {to}"


def apply_plan_response(name, message):
    """只有 Lead 对本队友当前计划的应答才会生效。"""
    metadata = message.get("metadata", {})
    request_id = metadata.get("request_id", "")
    with team_lock:
        assignment = teammate_assignments.get(name)
        work_version = assignment_versions.get(name, 0)
        task_id = str(assignment["task_id"]) if assignment else None
        state = pending_requests.get(request_id)
        valid = (
            message.get("from") == "lead"
            and message.get("to") == name
            and request_id == plan_request_ids.get(name)
            and state is not None
            and state.type == "plan_approval"
            and state.sender == name
            and state.target == "lead"
            and state.work_version == work_version
            and state.task_id == task_id
            and state.status in {"approved", "rejected"}
            and metadata.get("approve", False) == (state.status == "approved")
        )
        if not valid:
            return False, "[Ignored plan response: request mismatch]"
        plan_gates[name] = state.status
        active_teammates[name] = "working"
        plan_request_ids.pop(name, None)
        outcome = state.status
    return True, f"[Plan {outcome}] {message['content']}"


def apply_shutdown_request(name, message):
    """只接受 Lead 发给本队友、且仍处于 pending 的关机请求。"""
    request_id = message.get("metadata", {}).get("request_id", "")
    with team_lock:
        state = pending_requests.get(request_id)
        valid = (
            message.get("from") == "lead"
            and message.get("to") == name
            and state is not None
            and state.type == "shutdown"
            and state.sender == "lead"
            and state.target == name
            and state.status == "pending"
            and active_teammates.get(name) != "stopping"
        )
        if not valid:
            return False, "[Ignored shutdown request: request mismatch]"
        active_teammates[name] = "stopping"
    return True, request_id


def teammate_run_tool(name, tool_name, arguments, handlers):
    """队友工具的唯一入口:计划闸门 → 权限 hook → 执行。"""
    gate = plan_gates.get(name, "not_required")
    if tool_name in {"bash", "write_file", "edit_file"}:
        if gate not in {"not_required", "approved"}:
            return (
                f"Blocked: plan status is {gate}. Submit or revise the plan "
                "and wait for approval before changing the workspace."
            )
    handler = handlers.get(tool_name)
    if handler is None:
        return f"Unknown tool: {tool_name}"
    call = make_tool_call(tool_name)
    blocked = trigger_hooks("PreToolUse", call, arguments)
    if blocked is not None:
        return str(blocked)
    try:
        output = clean_text(handler(**arguments))
    except Exception as error:
        output = f"Error: {type(error).__name__}: {error}"
    trigger_hooks("PostToolUse", call, arguments, output)
    return output



#——————————————————————————————————————————————————————————————————
class TeammateRuntime:
    """一个常驻队友:独立 messages,在 WORK / IDLE 之间循环。"""

    def __init__(self, name, role, prompt, task_id=None, require_plan=False):
        """拼好系统提示词、首批消息和工具表。"""
        self.name = name
        self.system = (
            f"You are '{name}', a {role}. Use tools to complete the assigned "
            "Task, then call complete_task and report a concise result. "
            "If the first user message contains [Assigned task], that Task is "
            "already claimed; do not call claim_task for it again. "
            "When asked for a plan, call submit_plan and wait for approval "
            "before bash or file changes. File and shell tools use the Task's "
            "working directory; that directory is not a sandbox. The runtime "
            "delivers your final text to Lead. Use send_message only for "
            "intermediate coordination, and address the coordinator as 'lead'."
        )
        self.messages = [
            {"role": "system", "content": self.system},
            {"role": "user", "content": prompt},
        ]
        if task_id:
            task = TASKS.load(task_id)
            cwd = assignment_cwd(name)
            self.messages[1]["content"] += (
                f"\n\n[Assigned task {task.id}] {task.subject}\n"
                f"{task.description}\nWork directory: {cwd}"
            )
        if require_plan:
            self.messages[1]["content"] += (
                "\n\n[Plan required] Submit a plan and wait for Lead approval "
                "before changing files or using bash."
            )
        self.handlers = {
            "bash": self.tool_bash,
            "read_file": self.tool_read,
            "write_file": self.tool_write,
            "edit_file": self.tool_edit,
            "glob": self.tool_glob,
            "send_message": lambda to, content: teammate_send_message(name, to, content),
            "submit_plan": lambda plan: teammate_submit_plan(name, plan),
            "list_tasks": list_tasks,
            "claim_task": self.tool_claim,
            "complete_task": self.tool_complete,
        }

    def current_cwd(self):
        """取本队友当前的工作目录;没有认领任务就返回错误。"""
        if self.name not in teammate_assignments:
            return None, "Error: Claim a Task before using workspace tools."
        try:
            return assignment_cwd(self.name), None
        except (FileNotFoundError, ValueError) as error:
            return None, f"Error: Invalid task assignment: {error}"

    def tool_bash(self, command,run_in_background=False):
        """在任务目录里执行 shell 命令。"""
        cwd, error = self.current_cwd()
        return error or run_bash(command, cwd=cwd)

    def tool_read(self, path, limit=None):
        """读取任务目录里的文件。"""
        cwd, error = self.current_cwd()
        return error or run_read(path, limit=limit, cwd=cwd)

    def tool_write(self, path, content):
        """写入任务目录里的文件。"""
        cwd, error = self.current_cwd()
        return error or run_write(path, content, cwd=cwd)

    def tool_edit(self, path, old_text, new_text):
        """替换任务目录里的文件内容。"""
        cwd, error = self.current_cwd()
        return error or run_edit(path, old_text, new_text, cwd=cwd)

    def tool_glob(self, pattern):
        """在任务目录里按 glob 找文件。"""
        cwd, error = self.current_cwd()
        return error or run_glob(pattern, cwd=cwd)

    def tool_claim(self, task_id):
        """认领一个任务并把本队友的 cwd 绑上去。"""
        try:
            return team_claim_task(task_id, self.name)
        except (FileNotFoundError, ValueError) as error:
            return f"Error: {error}"

    def tool_complete(self, task_id):
        """完成本队友认领的任务。"""
        try:
            return complete_task(task_id, self.name)
        except (FileNotFoundError, ValueError) as error:
            return f"Error: {error}"

    def handle_inbox(self, inbox):
        """处理收件箱;收到有效关机请求返回 True。"""
        work_messages = []
        for message in inbox:
            msg_type = message.get("type", "message")
            if msg_type == "shutdown_request":
                accepted, notice = apply_shutdown_request(self.name, message)
                if not accepted:
                    work_messages.append(notice)
                    continue
                BUS.send(self.name, "lead", "Shutdown acknowledged.",
                         "shutdown_response",
                         {"request_id": notice, "approve": True})
                return True
            if msg_type == "plan_approval_response":
                _, notice = apply_plan_response(self.name, message)
                work_messages.append(notice)
                continue
            if msg_type == "plan_request":
                work_messages.append(f"[Plan required] {message['content']}")
                continue
            work_messages.append(
                f"[Message from {message['from']}] {message['content']}"
            )
        if work_messages:
            self.messages.append({
                "role": "user",
                "content": "\n".join(work_messages),
            })
        return False

    def work(self):
        """跑一轮队友模型调用;返回 continue / idle / stop。"""
        if self.handle_inbox(BUS.read_inbox(self.name)):
            return "stop"
        with team_lock:
            active_teammates[self.name] = "working"
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=self.messages,
                tools=TEAMMATE_TOOLS,
                tool_choice="auto",
            )
        except Exception as error:
            BUS.send(self.name, "lead",
                     f"{type(error).__name__}: {error}", "error")
            return "stop"

        message = response.choices[0].message
        tool_calls = message.tool_calls
        assistant_message = {
            "role": "assistant",
            "content": message.content or "",
        }
        if tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in tool_calls
            ]
        self.messages.append(assistant_message)

        if tool_calls:
            for tool_call in tool_calls:
                try:
                    arguments = parse_json(tool_call.function.arguments)
                except (json.JSONDecodeError, TypeError) as error:
                    output = f"Error: invalid tool arguments JSON: {error}"
                else:
                    output = teammate_run_tool(
                        self.name,
                        tool_call.function.name,
                        arguments,
                        self.handlers,
                    )
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": output,
                })
            return "continue"

        summary = (message.content or "").strip()
        gate = plan_gates.get(self.name, "not_required")
        if gate != "pending" and summary:
            BUS.send(self.name, "lead", summary, "result")
        if gate == "pending":
            with team_lock:
                active_teammates[self.name] = "waiting_approval"
        else:
            release_completed_assignment(self.name)
            with team_lock:
                active_teammates[self.name] = "idle"
            BUS.send(self.name, "lead", "Waiting for more work.",
                     "idle_notification")
        return "idle"

    def wait_for_work(self):
        """IDLE:先看收件箱,再看任务板;拿到活返回 True,该关机返回 False。"""
        while True:
            inbox = BUS.wait_for_messages(self.name, IDLE_SCAN_INTERVAL)
            if inbox:
                before = len(self.messages)
                if self.handle_inbox(inbox):
                    return False
                if len(self.messages) > before:
                    return True
                continue
            task = claim_next_task(self.name)
            if task is None:
                continue
            cwd = assignment_cwd(self.name)
            self.messages.append({
                "role": "user",
                "content": (
                    f"[Auto-claimed task {task.id}] {task.subject}\n"
                    f"{task.description}\nWork directory: {cwd}"
                ),
            })
            print(f"  [idle] {self.name} claimed {task.id}: {task.subject}")
            return True

    def run(self):
        """队友线程主体:WORK / IDLE 循环,直到关机或异常。"""
        set_interactive_allowed(False)
        try:
            state = "continue"
            while state != "stop":
                if state == "idle" and not self.wait_for_work():
                    break
                state = self.work()
        except Exception as error:
            try:
                BUS.send(self.name, "lead",
                         f"{type(error).__name__}: {error}", "error")
            except Exception:
                pass
        finally:
            try:
                release_teammate_assignment(self.name)
            except Exception as error:
                try:
                    BUS.send(self.name, "lead",
                             f"Assignment cleanup failed: "
                             f"{type(error).__name__}: {error}", "error")
                except Exception:
                    pass
            with team_lock:
                active_teammates.pop(self.name, None)
                plan_gates.pop(self.name, None)
                plan_request_ids.pop(self.name, None)
                teammate_threads.pop(self.name, None)
            print(f"  [teammate] {self.name} finished")





#——————————————————————————————————————————————————————————————
def spawn_teammate_thread(name, role, prompt, task_id=None, require_plan=False):
    """先认领初始任务,再启动一个常驻队友线程。"""
    if not VALID_AGENT_NAME.fullmatch(name or ""):
        return ("Invalid teammate name: use 1-64 letters, digits, "
                "underscores, or dashes")
    if name.lower() in RESERVED_TEAMMATE_NAMES:
        return f"Invalid teammate name: '{name}' is reserved by the runtime"
    with team_lock:
        if any(existing.casefold() == name.casefold()
               for existing in active_teammates):
            return f"Teammate '{name}' already exists"
        active_teammates[name] = "working"
        plan_gates[name] = "required" if require_plan else "not_required"
        assignment_versions[name] = 0

    if task_id:
        claimed = team_claim_task(task_id, name)
        if not claimed.startswith("Claimed "):
            with team_lock:
                active_teammates.pop(name, None)
                plan_gates.pop(name, None)
                assignment_versions.pop(name, None)
            return f"Cannot spawn teammate '{name}': {claimed}"

    runtime = TeammateRuntime(name, role, prompt, task_id, require_plan)
    thread = threading.Thread(target=runtime.run, daemon=True)
    with team_lock:
        teammate_threads[name] = thread
    thread.start()
    print(f"  [teammate] {name} spawned as {role}")
    assigned = f" for {task_id}" if task_id else " without an initial Task"
    return (
        f"Teammate '{name}' spawned as {role}{assigned}. "
        "End this turn; the runtime will deliver its events."
    )


def run_spawn_teammate(name, role, prompt, task_id=None, require_plan=False):
    """Lead 工具:启动一个常驻队友。"""
    return spawn_teammate_thread(name, role, prompt, task_id, require_plan)


def run_list_teammates():
    """Lead 工具:列出当前活跃队友和状态。"""
    with team_lock:
        if not active_teammates:
            return "No active teammates."
        return "\n".join(
            f"{name}: {status}"
            for name, status in sorted(active_teammates.items())
        )


def run_send_message(to, content):
    """Lead 工具:给某个队友发一条消息。"""
    with team_lock:
        if to not in active_teammates:
            return f"Teammate '{to}' is not active"
    BUS.send("lead", to, content)
    return f"Sent to {to}"


def run_request_shutdown(teammate):
    """Lead 工具:请求队友平滑关机。"""
    with team_lock:
        if teammate not in active_teammates:
            return f"Teammate '{teammate}' is not active"
        request_id = new_request_id()
        pending_requests[request_id] = ProtocolState(
            request_id=request_id,
            type="shutdown",
            sender="lead",
            target=teammate,
            status="pending",
            payload="",
        )
    BUS.send("lead", teammate, "Finish the current step and shut down.",
             "shutdown_request", {"request_id": request_id})
    return f"Shutdown requested from {teammate} ({request_id})"


def run_request_plan(teammate, task):
    """Lead 工具:要求在改工作区之前先交计划。"""
    with team_lock:
        if teammate not in active_teammates:
            return f"Teammate '{teammate}' is not active"
        plan_gates[teammate] = "required"
    BUS.send("lead", teammate, task, "plan_request")
    return f"Plan requested from {teammate}"


def run_review_plan(request_id, approve, feedback=""):
    """Lead 工具:批准或驳回某个待审计划。"""
    with team_lock:
        state = pending_requests.get(request_id)
        if not state:
            return f"Request {request_id} not found"
        if state.type != "plan_approval":
            return f"Request {request_id} is not a plan"
        if state.status != "pending":
            return f"Request {request_id} already {state.status}"
        assignment = teammate_assignments.get(state.sender)
        work_version = assignment_versions.get(state.sender, 0)
        task_id = str(assignment["task_id"]) if assignment else None
        if state.work_version != work_version or state.task_id != task_id:
            return f"Request {request_id} belongs to an earlier assignment"
        if plan_request_ids.get(state.sender) != request_id:
            return f"Request {request_id} is not the current plan"
        state.status = "approved" if approve else "rejected"
        sender = state.sender
        outcome = state.status
    content = feedback or (
        "Plan approved." if approve else "Revise the plan and submit it again."
    )
    BUS.send("lead", sender, content, "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    return f"Plan {outcome} ({request_id})"


def run_create_worktree(name, task_id):
    """Lead 工具:为任务创建并绑定 worktree(阶段 6 才实现)。"""
    return "Error: worktree support is not enabled (see stage 6)."