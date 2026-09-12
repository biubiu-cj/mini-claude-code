import json
import subprocess
from dataclasses import asdict

from background_task import BACKGROUND, kill_process_tree
from config import WORKDIR
from skill_loader import SKILL_LOADER
from taskboard import TASKS
from util import log_tag
from cron_scheduler import (
    cancel_cron,
    consume_cron_queue,
    list_crons,
    save_durable_jobs,
    schedule_cron,
)


def safe_path(path, cwd=None):
    """把路径解析到指定工作目录内,越界就报错;默认仓库根目录。"""
    base = (cwd or WORKDIR).resolve()
    p = (base / path).resolve()
    if not p.is_relative_to(base):
        raise ValueError("Path is outside WORKDIR")
    return p


#执行bash
def run_bash(command, cwd=None):
    """在指定目录执行 shell 命令,默认仓库根目录;超时 30 秒。"""
    print(f"\n{log_tag()}[Tool] {command}")
    process = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd or WORKDIR),
    )
    try:
        stdout, stderr = process.communicate(timeout=30)
        output = stdout
        if stderr:
            output += stderr
        return output
    except subprocess.TimeoutExpired:
        kill_process_tree(process)
        return "Command timed out after 30 seconds."
    except Exception as e:
        kill_process_tree(process)
        return str(e)


def start_background_task(command):
    """在后台启动 bash 任务,返回立即给模型的工具结果。"""
    task_id = BACKGROUND.start(command)
    print(f"\n[Background] {task_id} started: {command}")
    return (
        f"Background task {task_id} started: {command}\n"
        "It is running in the background; do not wait for it. "
        "A task notification with its output will be injected into the "
        "conversation when it finishes."
    )


def run_read(path, limit=None, cwd=None):
    """读取文件内容;默认仓库根目录。"""
    lines = safe_path(path, cwd).read_text(encoding="utf-8").splitlines()
    if limit:
        lines = lines[:limit]
    return "\n".join(lines)


def run_write(path, content, cwd=None):
    """写入文件;默认仓库根目录。"""
    safe_path(path, cwd).write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} bytes to {path}"


def run_edit(path, old_text, new_text, cwd=None):
    """替换文件中的一段文本;默认仓库根目录。"""
    text = safe_path(path, cwd).read_text(encoding="utf-8")
    if old_text not in text:
        return "Error: text not found"
    safe_path(path, cwd).write_text(
        text.replace(old_text, new_text, 1),
        encoding="utf-8"
    )
    return f"Edited {path}"


def run_glob(pattern, cwd=None):
    """按 glob 模式找文件;默认在仓库根目录下找,最多返回 200 条。"""
    import glob as g

    matches = sorted(set(g.glob(pattern, root_dir=cwd or WORKDIR, recursive=True)))

    shown = matches[:200]

    if len(matches) > 200:
        shown.append("... (more matches omitted; narrow the pattern)")

    return "\n".join(shown)


def run_todo_write(todos, todo_manager):
    """把模型给的计划写进本会话的 TodoManager。"""
    output = todo_manager.update(todos)
    print(f"\n[Tool] Updated todos:\n{output}")
    return output


def run_load_skill(name):
    """按名字取出某个技能的全文。"""
    output = SKILL_LOADER.load(name)

    print(f"\n[Tool] Loaded skill: {name}")

    return output


def create_task(subject: str, description: str = "") -> str:
    """建一条持久任务,返回它的 JSON。"""
    task = TASKS.create(subject, description)
    return json.dumps(asdict(task), ensure_ascii=False, indent=2)


def update_task(task_id: str, add_blocked_by: list[str]) -> str:
    """给一个 pending 任务补前置依赖,返回更新后的 JSON。"""
    task = TASKS.update_dependencies(task_id, add_blocked_by)
    return json.dumps(asdict(task), ensure_ascii=False, indent=2)


def list_tasks() -> str:
    """列出所有持久任务,每行一条。"""
    tasks = TASKS.list()
    lines = []
    for task in tasks:
        blocked = ", ".join(task.blocked_by)
        lines.append(
            f"{task.id} | {task.status} | {task.subject} | "
            f"owner={task.owner} | blocked_by=[{blocked}]"
        )
    if not lines:
        return "No tasks."
    return "\n".join(lines)


def get_task(task_id: str) -> str:
    """按 id 读一条任务的完整 JSON。"""
    task = TASKS.load(task_id)
    return json.dumps(asdict(task), ensure_ascii=False, indent=2)


#任务认领
def claim_task(task_id: str, owner: str) -> str:
    """把一条 pending 任务认领给自己;前置未完成则拒绝。"""
    tasks_by_id = TASKS.load_all()
    task = TASKS.load(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    dependencies = TASKS.incomplete_dependencies(task, tasks_by_id)
    if dependencies:
        return f"Blocked by: {', '.join(dependencies)}"
    task.owner = owner
    task.status = "in_progress"
    TASKS.save(task)
    return f"Claimed {task_id} ({task.subject})"


#完成任务
def complete_task(task_id: str, owner: str) -> str:
    """完成自己认领的任务,并报告因此解除阻塞的任务。"""
    # 一次读盘拿到全部任务,避免下面反复 list()/can_start() 造成的重复 IO
    tasks_by_id = TASKS.load_all()
    task = TASKS.load(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    if task.owner != owner:
        return f"Task {task_id} is owned by {task.owner}, not {owner}"

    def ready_ids():
        """当前所有"有前置且前置都已完成"的任务 id。"""
        return {
            task_item.id
            for task_item in tasks_by_id.values()
            if task_item.status == "pending" and task_item.blocked_by
            and not TASKS.incomplete_dependencies(task_item, tasks_by_id)
        }

    ready_before = ready_ids()
    task.status = "completed"
    tasks_by_id[task.id] = task
    TASKS.save(task)
    unblocked = [
        task_item.subject
        for task_item in tasks_by_id.values()
        if task_item.status == "pending" and task_item.blocked_by
        and task_item.id not in ready_before
        and not TASKS.incomplete_dependencies(task_item, tasks_by_id)
    ]
    message = f"Completed {task_id} ({task.subject})"
    if unblocked:
        message += "\nUnblocked: " + ", ".join(unblocked)
    return message


def run_schedule_cron(cron, prompt, recurring=True, durable=False):
    """Agent 工具 schedule_cron:建定时任务,返回可读的任务 id。"""
    job_id = schedule_cron(cron, prompt, recurring=recurring, durable=durable)
    kept = "recurring" if recurring else "one-shot"
    persistence = "durable" if durable else "in-memory"
    return f"Scheduled {job_id} ({kept}, {persistence}): {cron} -> {prompt}"


def run_cancel_cron(job_id):
    """Agent 工具 cancel_cron:把布尔翻成人话,否则工具结果是 'True'。"""
    if cancel_cron(job_id):
        return f"Cancelled {job_id}"
    return f"No scheduled job with id {job_id}"
