# owned by cj
"""s16 Workflow:一次 tool_use 跑完一整套可续跑的脚本化编排。

模型只提供 workflow 名称 / 参数 / run ID,可执行代码永远来自宿主注册表。
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from config import BASE_DIR, MODEL, client

# 产物目录:和 .tasks / .mailboxes / .worktrees 一致,都挂在脚本所在目录
STORE = BASE_DIR / ".workflows"

# 每个 run 的子 agent 硬上限,以及并发上限
AGENT_CAP = 1000
CONCURRENCY = 8

# journal 缓存未命中的哨兵。必须是独立对象:合法的缓存值也可能是 None
MISS = object()

# meta.name 会拼进文件名,所以必须是安全 slug
WORKFLOW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# runId 是模型可以回传的,同样当不可信输入
RUN_ID_RE = re.compile(r"^wf_[A-Za-z0-9][A-Za-z0-9._-]{0,63}_[0-9a-f]{16}$")


class WorkflowInputError(Exception):
    """workflow 代码 / 元数据 / schema 输入不合法。"""


def _stable_hash(text: str) -> int:
    """进程稳定的哈希。

    不能用内置 hash():它按进程加盐,`run` 和 `resume` 是两次进程,
    算出来的 key 会完全不同 —— resume 会全部 miss。
    """
    return int(hashlib.sha256(text.encode()).hexdigest(), 16)


def create_run_id(meta) -> str:
    """按 workflow 名 + 随机后缀造一个 runId 字面量。"""
    return f"wf_{meta['name']}_{secrets.token_hex(8)}"


def validate_run_id(run_id):
    """校验 runId 形状;模型传回来的值一律当成不可信输入。"""
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise WorkflowInputError("invalid workflow runId")
    return run_id


def reserve_run_id(meta) -> str:
    """在打开 journal 之前,用排他创建原子地占住一个新的 runId。"""
    STORE.mkdir(parents=True, exist_ok=True)
    for _ in range(32):
        run_id = validate_run_id(create_run_id(meta))
        snapshot_path = STORE / f"{run_id}.json"
        try:
            handle = os.open(snapshot_path,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(handle)
        return run_id
    raise WorkflowInputError("could not allocate a unique workflow runId")


def create_task_id(run_id) -> str:
    """任务 id 直接由 runId 派生,便于两边对照。"""
    return f"local_workflow_{run_id}"


def validate_meta(meta):
    """执行任何 workflow 代码之前,先校验注册的元数据。"""
    if not isinstance(meta, dict):
        raise WorkflowInputError("meta must be an object")
    if not meta.get("name") or not meta.get("description"):
        raise WorkflowInputError("meta must contain name and description")
    if not isinstance(meta["name"], str) or not WORKFLOW_NAME_RE.fullmatch(meta["name"]):
        raise WorkflowInputError("meta.name must be a 1-64 char safe slug")
    if "phases" in meta and (
        not isinstance(meta["phases"], list)
        or not all(isinstance(part, str) and part for part in meta["phases"])
    ):
        raise WorkflowInputError("meta.phases must be non-empty strings")
    return meta



#记录workflow 日记
class WorkflowJournal:
    """append-only 的 <runId>.journal.jsonl。
    resume 时重新执行脚本,但 key 已在 journal 里的 agent() 直接用缓存。
    """
    def __init__(self, run_id, resume, store=None):
        """新建 journal;resume 模式下先把旧记录读进 cache。"""
        store = STORE if store is None else store
        store.mkdir(parents=True, exist_ok=True)
        self.path = store / f"{run_id}.journal.jsonl"
        self.cache = {}
        if resume:
            if not self.path.exists():
                raise WorkflowInputError(f"resume journal not found for {run_id}")
            for line_number, line in enumerate(
                    self.path.read_text(encoding="utf-8").splitlines(), start=1):
                try:
                    record = json.loads(line)
                    if (not isinstance(record, dict)
                            or not isinstance(record.get("key"), str)
                            or "value" not in record):
                        raise ValueError("expected key/value record")
                except (json.JSONDecodeError, ValueError) as error:
                    raise WorkflowInputError(
                        f"invalid resume journal record at line {line_number}"
                    ) from error
                self.cache[record["key"]] = record["value"]
            self._handle = self.path.open("a", encoding="utf-8")
        else:
            # 新运行截断旧文件 —— 所以 reserve_run_id 必须在打开之前占住 id
            self._handle = self.path.open("w", encoding="utf-8")

    def key(self, kind, label, prompt, schema):
        """由调用内容算出的语义 key,与并发完成顺序无关。
        parallel / pipeline 完成顺序不定,用计数器当 key 会让两次跑的缓存整体错位。
        """
        basis = json.dumps([kind, label, prompt, schema], sort_keys=True,
                           ensure_ascii=False, default=str)
        return f"{kind}-{_stable_hash(basis) % 10**10:010d}"

    def cached(self, key):
        """取缓存;没有就返回 MISS 哨兵(不能用 None,合法值可能是 None)。"""
        return self.cache.get(key, MISS)

    def record(self, key, value):
        """追加一条记录并立刻 flush,保证进程被杀也不丢已完成的结果。"""
        self._handle.write(json.dumps({"key": key, "value": value}) + "\n")
        self._handle.flush()
        self.cache[key] = value

    def close(self):
        """关掉 journal 文件。"""
        self._handle.close()





#子agent放回的内容的格式
class SimpleJsonSchema:
    """够用的 JSON Schema 子集:type / properties / required / items / enum。"""
    def __init__(self, schema):
        """记下 schema。"""
        self.schema = schema

    SCALARS = {
        "string": lambda v: isinstance(v, str),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
    }

    def validate(self, value, schema=None, path="$"):
        """校验 value;返回 (是否通过, 带字段路径的错误说明)。"""
        schema = self.schema if schema is None else schema
        if not isinstance(schema, dict):
            return True, None
        expected = schema.get("type")
        if expected == "object":
            return self._check_object(value, schema, path)
        if expected == "array":
            return self._check_array(value, schema, path)
        if expected in self.SCALARS and not self.SCALARS[expected](value):
            return False, (f"{path}: expected {expected}, "
                           f"got {type(value).__name__}")
        if "enum" in schema and value not in schema["enum"]:
            return False, f"{path}: {value!r} not in enum {schema['enum']}"
        return True, None

    def _check_object(self, value, schema, path):
        """校验对象:required 齐、properties 逐个递归。
        错误信息带字段路径 —— 只说"expected object"在排查 workflow 时没用。
        """
        if not isinstance(value, dict):
            return False, f"{path}: expected object, got {type(value).__name__}"
        for name in schema.get("required", []):
            if name not in value:
                return False, f"{path}: missing required field {name!r}"
        for name, sub in (schema.get("properties") or {}).items():
            if name in value:
                ok, error = self.validate(value[name], sub, f"{path}.{name}")
                if not ok:
                    return False, error
        return True, None

    def _check_array(self, value, schema, path):
        """校验数组:每项都过 items,错误路径带下标。"""
        if not isinstance(value, list):
            return False, f"{path}: expected array, got {type(value).__name__}"
        item_schema = schema.get("items")
        if item_schema is None:
            return True, None
        for index, item in enumerate(value):
            ok, error = self.validate(item, item_schema, f"{path}[{index}]")
            if not ok:
                return False, error
        return True, None


@dataclass
class RunnerOutput:
    """一次子 agent 调用的结果:解析后的值 + token 用量。"""
    value: object
    tokens: int = 0


def _parse_runner_json(text: str) -> object:
    """从模型回复里抠出一个 JSON 值;容忍 ``` 围栏和前后废话。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for position, character in enumerate(stripped):
            if character != "{":
                continue
            try:
                value, _ = decoder.raw_decode(stripped[position:])
            except json.JSONDecodeError:
                continue
            return value
        raise WorkflowInputError("workflow agent returned invalid JSON")


class AgentRunner:
    """workflow 的叶子执行器:一次 API 调用,不挂任何工具。"""
    def __init__(self, client, model):
        """记下客户端和模型名。"""
        self.client = client
        self.model = model

    def run(self, prompt, schema=None, label=None):
        """跑一个子 agent;有 schema 就要求它只返回匹配的 JSON。"""
        request = prompt
        if schema is not None:
            request += (
                "\n\nReturn only one JSON object matching this schema:\n"
                + json.dumps(schema, ensure_ascii=True, sort_keys=True)
            )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": (
                    "You are a focused workflow agent. Complete only the "
                    "supplied step. Do not claim access to files or results "
                    "not included in the prompt."
                )},
                {"role": "user", "content": request},
            ],
            max_tokens=2000,
        )
        text = response.choices[0].message.content or ""
        if schema is None:
            value = text
        else:
            try:
                value = _parse_runner_json(text)
            except WorkflowInputError:
                # 先别抛:交给 ExecutionState 的 schema 校验去触发重试
                value = text
        usage = getattr(response, "usage", None)
        tokens = int(getattr(usage, "prompt_tokens", 0) or 0) + int(
            getattr(usage, "completion_tokens", 0) or 0)
        return RunnerOutput(value, tokens)


# 造 runner 的工厂。默认指向真实模型,测试时可以整体换掉
RUNNER_FACTORY = lambda: AgentRunner(client, MODEL)

class ExecutionLimits:
    """整个 run 共享的硬上限与并发闸门。"""
    def __init__(self):
        """建 agent 计数器和并发信号量。"""
        self.agents = 0
        self.semaphore = asyncio.Semaphore(CONCURRENCY)

    def claim_agent(self):
        """占一个 agent 名额;超上限就报错。"""
        self.agents += 1
        if self.agents > AGENT_CAP:
            raise WorkflowInputError(f"agent() cap of {AGENT_CAP} exceeded")


class ExecutionState:
    """注入 workflow 脚本的编排上下文。"""
    def __init__(self, task, journal, runner, args, depth=0, limits=None):
        """记下 task / journal / runner / args,以及嵌套深度和共享上限。"""
        self.task = task
        self.journal = journal
        self.runner = runner
        self.args = args
        self._depth = depth
        self._phase = None
        self._phases_seen = set()
        self._limits = limits or ExecutionLimits()

    def phase(self, title):
        """进入一个阶段;同一阶段重复出现(比如每个 pipeline item 都调)不重复播报。"""
        self._phase = title
        if title not in self._phases_seen:
            self._phases_seen.add(title)
            self.task.progress_event("workflow_phase", title=title)

    def log(self, message):
        """打一行 workflow 进度日志。"""
        self.task.progress_event("workflow_log", message=message)

    async def agent(self, prompt, schema=None, label=None, phase=None):
        """派一个子 agent。带 schema 就强制结构化输出并校验(失败重试一次)。
        resume 时 key 命中缓存就直接返回,不跑模型。
        """
        label = label or (prompt[:24] + "...")
        key = self.journal.key("agent", label, prompt, schema)

        cached = self.journal.cached(key)
        if cached is not MISS:
            if schema is not None:
                ok, error = SimpleJsonSchema(schema).validate(cached)
                if not ok:
                    raise WorkflowInputError(
                        f"cached agent output failed schema validation: {error}")
            self.task.progress_event("workflow_agent", label=label,
                                     phase=phase or self._phase, status="cached")
            return cached

        # 真正要跑模型:这才占额度与并发名额,命中缓存的调用两者都不该吃
        self._limits.claim_agent()
        async with self._limits.semaphore:
            run = await asyncio.to_thread(self.runner.run, prompt, schema, label)
        result, tokens = run.value, run.tokens

        if schema is not None:
            ok, error = SimpleJsonSchema(schema).validate(result)
            if not ok:
                retry = await asyncio.to_thread(
                    self.runner.run, prompt + "\n\nReturn valid JSON.",
                    schema, label)
                result = retry.value
                tokens += retry.tokens
                ok, error = SimpleJsonSchema(schema).validate(result)
                if not ok:
                    raise WorkflowInputError(
                        f"agent({{schema}}) invalid output: {error}")

        self.task.usage["agents"] += 1
        self.task.usage["tokens"] += tokens
        self.journal.record(key, result)      # 只有成功才落 journal
        self.task.progress_event("workflow_agent", label=label,
                                 phase=phase or self._phase, status="done")
        return result

    async def parallel(self, thunks):
        """等齐屏障:全部 thunk 并发跑完,一起等结果;任一失败即整体失败。"""
        return await asyncio.gather(*[thunk() for thunk in thunks])

    async def pipeline(self, items, *stages):
        """每个 item 独立跑完全部 stage,**阶段之间不等齐**。
        stage 收到 (上一步结果, 原始 item, 下标)。
        """
        async def run_item(item, index):
            """把一个 item 依次喂给所有 stage。"""
            value = item
            for stage in stages:
                value = await stage(value, item, index)
            return value
        return await asyncio.gather(
            *[run_item(item, index) for index, item in enumerate(items)])

    async def workflow(self, name, args=None):
        """内联跑一个子 workflow(只允许一层),共享本 run 的 journal 和上限。"""
        if self._depth >= 1:
            raise WorkflowInputError("workflow() nesting is one level only")
        if name not in WORKFLOWS:
            raise WorkflowInputError(f"unknown workflow '{name}'")
        meta, script_fn = WORKFLOWS[name]
        child = ExecutionState(self.task, self.journal, self.runner,
                               args or {}, depth=self._depth + 1,
                               limits=self._limits)
        return await script_fn(child, args or {})



_run_locks_guard = threading.Lock()
_run_locks: dict[str, threading.Lock] = {}


@contextmanager
def workflow_run_lock(run_id):
    """整次执行期间持有 run lock;跨线程 + 跨进程都独占。"""
    # 取锁和抢锁必须在同一把 guard 下完成:否则"摘表项"和"抢到旧锁对象"之间
    # 有个窗口,两个线程会各拿到一把不同的 Lock,进程内互斥就失效了
    with _run_locks_guard:
        local_lock = _run_locks.get(run_id)
        if local_lock is None:
            local_lock = threading.Lock()
            _run_locks[run_id] = local_lock
        if not local_lock.acquire(blocking=False):
            raise WorkflowInputError(f"run {run_id} is already executing")
    lock_path = STORE / f"{run_id}.lock"
    try:
        STORE.mkdir(parents=True, exist_ok=True)
        handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # 和下面 finally 同理:摘表项和释放必须一起做,否则"撞过锁"的 runId
        # 会在 _run_locks 里留一个永久条目 —— 正是长驻进程要避免的那种泄漏
        with _run_locks_guard:
            _run_locks.pop(run_id, None)
            local_lock.release()
        raise WorkflowInputError(f"run {run_id} is locked by another process")
    try:
        os.close(handle)
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        # 先摘表项再释放,且都在 guard 里 —— 后来者拿到的一定是新锁。
        # 不摘的话,长驻进程会为每个见过的 runId 永久留一个 Lock 对象
        with _run_locks_guard:
            _run_locks.pop(run_id, None)
            local_lock.release()


@dataclass
class LocalWorkflowTask:
    """一次 workflow 运行的状态载体:状态 + 用量 + 进度事件流。"""
    task_id: str
    run_id: str
    meta: dict
    status: str = "running"
    usage: dict = field(default_factory=lambda: {"agents": 0, "tokens": 0})
    progress: list = field(default_factory=list)

    def event(self, name, **data):
        """记一条生命周期事件。"""
        self.progress.append({"type": name, **data})

    def progress_event(self, ptype, **data):
        """记一条进度事件(阶段切换 / 子 agent 启停 / 日志)。"""
        self.event(ptype, **data)


def serialize_task(task):
    """把任务对象压成可写进 JSON 的形状。"""
    return {
        "taskId": task.task_id, "taskType": "local_workflow",
        "runId": task.run_id, "workflowName": task.meta["name"],
        "status": task.status, "usage": dict(task.usage),
        "progress": list(task.progress),
    }


def _write_json(path, value):
    """原子写 JSON:临时文件 + os.replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    default=str), encoding="utf-8")
    os.replace(temporary, path)


def _read_snapshot(run_id):
    """读回一次运行的快照;缺失或损坏都当输入错误,不让 JSONDecodeError 漏出去。"""
    path = STORE / f"{run_id}.json"
    if not path.exists():
        raise WorkflowInputError(f"snapshot not found for {run_id}")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        # reserve_run_id 留下的 0 字节占位、或写到一半被杀,都会落到这里
        raise WorkflowInputError(
            f"snapshot for {run_id} is unreadable: {error}") from error
    if not isinstance(snapshot, dict):
        raise WorkflowInputError(f"snapshot for {run_id} is not an object")
    return snapshot


def check_permission(meta, settings=None):
    """宿主侧权限钩子。workflow 不读写工作区,所以这里只留扩展点。"""
    return True




class WorkflowTool:
    """Workflow 工具本体:校验 meta、过权限、占 runId、跑脚本、发事件。"""
    async def call(self, meta, script_fn, args=None, resume_from_run_id=None):
        """入口:校验 → 占位/校验 runId → 持锁执行。"""
        validate_meta(meta)
        if not check_permission(meta):
            raise WorkflowInputError(f"workflow '{meta['name']}' is not permitted")
        resuming = resume_from_run_id is not None
        run_id = (validate_run_id(resume_from_run_id) if resuming
                  else reserve_run_id(meta))
        with workflow_run_lock(run_id):
            return await self._call_locked(
                meta, script_fn, args, run_id, resuming)

    async def _call_locked(self, meta, script_fn, args, run_id, resuming):
        """持锁的实际执行:建 journal、跑脚本、写产物、发通知。"""
        if resuming:
            snapshot = _read_snapshot(run_id)
            if snapshot.get("workflowName") != meta["name"]:
                raise WorkflowInputError("resume runId does not match workflow meta")
            saved_args = snapshot.get("args", {})
            if args is None:
                args = saved_args
            elif args != saved_args:
                # 参数变了就必须报错:journal 的缓存是按旧参数算的 key,
                # 静默复用会把旧结果当成新参数的结果返回
                raise WorkflowInputError("resume args do not match the original run")
            journal = WorkflowJournal(run_id, resume=True)
        else:
            args = args or {}
            journal = WorkflowJournal(run_id, resume=False)

        task = LocalWorkflowTask(create_task_id(run_id), run_id, meta)
        launched = {"status": "async_launched", "taskId": task.task_id,
                    "taskType": "local_workflow", "runId": run_id,
                    "workflowName": meta["name"]}
        task.event("async_launched", runId=run_id, taskId=task.task_id)
        task.event("task_started", workflow=meta["name"],
                   phases=",".join(meta.get("phases", [])) or "-",
                   resume=resuming)
        _write_json(STORE / f"{run_id}.json",
                    {"runId": run_id, "workflowName": meta["name"],
                     "args": args, "task": serialize_task(task)})

        try:
            ctx = ExecutionState(task, journal, RUNNER_FACTORY(), args)
            result = await script_fn(ctx, args)
            task.status = "completed"
        except Exception as error:
            task.status = "failed"
            result = {"error": str(error)}
        finally:
            journal.close()

        _write_json(STORE / f"{run_id}.output.json", result)
        _write_json(STORE / f"{run_id}.json",
                    {"runId": run_id, "workflowName": meta["name"],
                     "args": args, "task": serialize_task(task)})
        task.event("task_notification", status=task.status,
                   agents=task.usage["agents"], tokens=task.usage["tokens"],
                   outputFile=f".workflows/{run_id}.output.json")
        return {"launched": launched, "result": result, "task": task}



async def run_workflow(name, args=None, resume_from_run_id=None):
    """模型侧的适配器:用名称去宿主注册表查可信代码。"""
    if not isinstance(name, str):
        raise WorkflowInputError("workflow name must be a string")
    if name not in WORKFLOWS:
        available = ", ".join(sorted(WORKFLOWS)) or "(none)"
        raise WorkflowInputError(f"unknown workflow '{name}'. Available: {available}")
    if args is not None and not isinstance(args, dict):
        raise WorkflowInputError("workflow args must be an object")
    meta, script_fn = WORKFLOWS[name]
    out = await WorkflowTool().call(meta, script_fn, args=args,
                                    resume_from_run_id=resume_from_run_id)
    return {"launched": out["launched"], "result": out["result"],
            "task": serialize_task(out["task"])}


def run_workflow_sync(**tool_input):
    """把同步的宿主分发器桥到异步的 workflow 运行时。"""
    try:
        return json.dumps(asyncio.run(run_workflow(**tool_input)), default=str)
    except WorkflowInputError as error:
        return f"Error: {error}"


FINDINGS_SCHEMA = {
    "type": "object", "required": ["findings"],
    "properties": {"findings": {"type": "array", "items": {
        "type": "object", "required": ["title", "detail"],
        "properties": {"title": {"type": "string"},
                       "detail": {"type": "string"}}}}},
}

VERDICT_SCHEMA = {
    "type": "object", "required": ["isReal"],
    "properties": {"isReal": {"type": "boolean"},
                   "why": {"type": "string"}},
}

SAMPLE_META = {"name": "review-changes", "description": "审查代码改动",
               "phases": ["Review", "Verify"]}

DIMENSIONS = ["correctness", "security", "performance", "style"]


async def sample_workflow(ctx, args):
    """示例:每个维度独立走 审计 → 验证,最后合并确认的问题。"""
    ctx.phase("Review")
    changes = args.get("changes", "")

    async def audit(_value, dimension, _index):
        """审一个维度;先给这一维跑一条 pipeline 的 audit 阶段。"""
        out = await ctx.agent(
            f"检查这段变更里有没有 {dimension} 相关的问题:\n{changes}",
            schema=FINDINGS_SCHEMA, label=f"audit:{dimension}", phase="Review")
        return {"dimension": dimension, "findings": out["findings"]}

    async def verify(audited, dimension, _index):
        """对这一维的每条发现,各起一个对抗性验证子 agent(等齐屏障)。"""
        ctx.phase("Verify")
        verdicts = await ctx.parallel([
            (lambda finding=finding: ctx.agent(
                f"根据变更内容验证这条 finding:\n{changes}\n\n{finding}",
                schema=VERDICT_SCHEMA,
                label=f"verify:{dimension}:{finding['title']}"))
            for finding in audited["findings"]
        ])
        confirmed = [finding for finding, verdict
                     in zip(audited["findings"], verdicts)
                     if verdict and verdict["isReal"]]
        return {"dimension": dimension, "confirmed": confirmed}

    results = await ctx.pipeline(DIMENSIONS, audit, verify)
    confirmed = [finding for result in results if result
                 for finding in result["confirmed"]]
    ctx.log(f"确认了 {len(confirmed)} 个真实问题")
    return {"confirmed": confirmed}


# 宿主注册表:模型只能按名字查,拿不到函数本身
WORKFLOWS = {SAMPLE_META["name"]: (SAMPLE_META, sample_workflow)}