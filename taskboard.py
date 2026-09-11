from contextlib import contextmanager
import json
import os
import re
import time
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from config import BASE_DIR


@dataclass
class Task:
    """一条持久任务记录;worktree 是可选的工作目录绑定。"""

    id: str
    subject: str
    description: str
    status: str
    blocked_by: list[str]
    owner: str | None = None
    created_at: float = 0.0
    worktree: str | None = None   # 可选的工作目录绑定


class TaskStore:
    """把任务以 JSON 存成一个目录,提供建、读、列、依赖与环检查。"""

    def __init__(self, directory):
        """记住任务目录,不存在就建出来。"""
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    #根据任务id找到对应的json
    def task_path(self, task_id):
        """把任务 id 解析成 json 文件路径,id 不合法就报错。"""
        if not self.valid_task_id(task_id):
            raise ValueError(f"Invalid task id: {task_id}")
        return self.directory / f"{task_id}.json"

    #校验id是否符合 task_+8位16进制
    def valid_task_id(self, task_id):
        """id 是否形如 task_ 加 8 位十六进制。"""
        return bool(re.fullmatch(r"task_[0-9a-f]{8}", task_id))

    #生成task id
    def generate_task_id(self):
        """生成一个磁盘上还没被占用的任务 id。"""
        while True:
            task_id = "task_" + os.urandom(4).hex()
            if not self.task_path(task_id).exists():
                return task_id

    #创建任务
    def create(self, subject, description=""):
        """新建一条 pending 任务并落盘。"""
        subject = str(subject).strip()

        if not subject:
            raise ValueError("Task subject cannot be empty")

        task = Task(
            id=self.generate_task_id(),
            subject=subject,
            description=str(description),
            status="pending",
            blocked_by=[],
            owner=None,
            created_at=time.time(),
        )

        self.save(task)
        return task

    #保存task到磁盘上去
    def save(self, task):
        """先写临时文件再原子替换,避免并发/崩溃留下半截 JSON。"""
        path = self.task_path(task.id)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(asdict(task), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return task

    #读取并校验一个task文件(load 和 list 共用)
    def read_task(self, path):
        """读取并校验一个任务文件,只保留 Task 认识的字段。"""
        data = json.loads(path.read_text(encoding="utf-8"))
        required = ("id", "subject", "description", "status", "blocked_by")
        missing = [name for name in required if name not in data]
        if missing:
            raise ValueError(
                f"Invalid task file {path.name}: missing {', '.join(missing)}"
            )
        if not self.valid_task_id(str(data["id"])):
            raise ValueError(f"Invalid task file {path.name}: bad task id")
        # 只取已知字段,文件里多出无关键时不会让 Task(**data) 报 TypeError
        known = set(Task.__dataclass_fields__)
        return Task(**{key: value for key, value in data.items() if key in known})

    #读取一个task依据task id
    def load(self, task_id):
        """按 id 读一个任务;文件不存在就报错。"""
        path = self.task_path(task_id)
        if not path.exists():
            raise FileNotFoundError(f"Task not found: {task_id}")
        return self.read_task(path)

    #一次性把所有task读进内存,避免同一步里反复读盘
    def load_all(self):
        """把所有任务读进内存,坏文件静默跳过。"""
        tasks = {}
        for path in self.directory.glob("task_*.json"):
            try:
                task = self.read_task(path)
            except Exception:
                continue
            tasks[task.id] = task
        return tasks

    #列举所有的task
    def list(self):
        """按创建时间列出全部任务。"""
        tasks = list(self.load_all().values())
        # 按创建时间排序:task id 是随机 hex,按 id 排等于随机顺序
        tasks.sort(key=lambda task: (task.created_at, task.id))
        return tasks

    #一个任务前面还有多少任务需要去完成
    def incomplete_dependencies(self, task, tasks_by_id=None):
        """列出这个任务还没完成的前置任务。"""
        if tasks_by_id is None:
            tasks_by_id = self.load_all()
        incomplete = []
        for dependency_id in task.blocked_by:
            dependency = tasks_by_id.get(dependency_id)
            # 依赖不存在时同样视为未完成,避免任务被误判为可开始
            if dependency is None or dependency.status != "completed":
                incomplete.append(dependency_id)
        return incomplete

    #判断一个任务是否可以开始依据task id
    def can_start(self, task_id, tasks_by_id=None):
        """这个任务的前置是不是都完成了。"""
        if tasks_by_id is None:
            tasks_by_id = self.load_all()
        task = tasks_by_id.get(task_id)
        if task is None:
            task = self.load(task_id)
        return not self.incomplete_dependencies(task, tasks_by_id)

    #判断给 task_id 加入 blocked_by 这些新依赖后,是否会让它落入环中
    def has_cycle(self, task_id, blocked_by, tasks_by_id=None):
        """加上这批新依赖后会不会成环。"""
        if tasks_by_id is None:
            tasks_by_id = self.load_all()
        graph = {tid: list(item.blocked_by) for tid, item in tasks_by_id.items()}
        graph.setdefault(task_id, [])

        # 只看新加的边:新产生的环必然经过其中至少一条。
        # 若拿整张图做环检测,别处一个无关的环也会让它误报
        def reaches(start, target, seen):
            """从 start 出发能不能走到 target。"""
            stack = [start]
            while stack:
                current = stack.pop()
                if current == target:
                    return True
                if current in seen:
                    continue
                seen.add(current)
                stack.extend(graph.get(current, []))
            return False

        for dependency_id in blocked_by:
            if dependency_id == task_id:
                return True
            if reaches(dependency_id, task_id, set()):
                return True
        return False

    #更新依赖关系
    def update_dependencies(self, task_id, add_blocked_by):
        """给一个还没被认领的 pending 任务补前置依赖。"""
        task = self.load(task_id)
        if task.status != "pending":
            raise ValueError("Only pending tasks can update dependencies")
        if task.owner is not None:
            raise ValueError("Cannot update dependencies after task is claimed")
        tasks_by_id = self.load_all()
        dependency_ids = list(task.blocked_by)
        for dependency_id in add_blocked_by:
            if dependency_id == task_id:
                raise ValueError("Task cannot depend on itself")
            if not self.valid_task_id(dependency_id):
                raise ValueError(f"Invalid task id: {dependency_id}")
            if dependency_id not in tasks_by_id:
                raise FileNotFoundError(f"Task not found: {dependency_id}")
            if dependency_id not in dependency_ids:
                dependency_ids.append(dependency_id)
        # 只传新增的依赖;has_cycle 按"新加的边"判断,不会误伤无关的环
        if self.has_cycle(task_id, add_blocked_by, tasks_by_id):
            raise ValueError("Adding dependencies would create a cycle")
        task.blocked_by = dependency_ids
        self.save(task)
        return task


TASKS_DIR = BASE_DIR / ".tasks"
TASKS = TaskStore(TASKS_DIR)

# 进程内任务存储锁:调用方用 `with task_store_lock():` 包住"读-判断-写"整段,
# 只锁住写那一步是不够的——两个线程还是会同时通过 pending 检查
_store_lock = threading.RLock()


@contextmanager
def task_store_lock():
    """序列化任务存储的读-改-写,防止并发认领同一个任务。"""
    with _store_lock:
        yield
