from dataclasses import dataclass
from datetime import datetime
import json
import os
import threading
import time

from config import BASE_DIR


# 1. CronJob：表示一个定时任务

@dataclass
class CronJob:
    """定时任务:定义(何时跑、跑什么)加交付状态。"""
    id: str                        # 定时任务的唯一 ID
    cron: str                      # Cron 表达式,决定任务什么时候触发
    prompt: str                    # 到时间后交给 Agent 执行的任务描述
    recurring: bool                # True=执行后保留;False=执行一次后删除
    durable: bool                  # True=写盘,重启后恢复;False=只在内存
    pending_delivery: bool = False # 已经到时间,正在等待 Agent 接收
    last_fired: str | None = None   # 上次触发的时间,防止同一分钟被重复触发


# 2. 全局数据

scheduled_jobs = {}        # job_id -> CronJob,所有定时任务
cron_lock = threading.Lock()   # 保护 scheduled_jobs
cron_queue = []            # 已到期、等待 Agent 执行的任务
cron_queue_lock = threading.Lock()   # 保护 cron_queue
job_counter = 0            # 生成任务 ID 用

# durable 任务的落盘位置:固定在脚本所在目录(基础设施,同 .tasks 的思路)
SCHEDULE_FILE = BASE_DIR / ".scheduled_tasks.json"


# 3. Cron 表达式匹配
# 支持的字段写法:*, */N, N, N-M, N,M

def cron_matches(cron: str, moment: datetime) -> bool:
    """判断时间点是否符合 5 段 cron 表达式。"""
    fields = cron.split()
    if len(fields) != 5:
        return False
    minute, hour, day, month, weekday = fields
    # 标准 cron 语义:周日=0 ... 周六=6;而 datetime.weekday() 是周一=0
    weekday_value = (moment.weekday() + 1) % 7
    return (
        match_field(minute, moment.minute, 0, 59)
        and match_field(hour, moment.hour, 0, 23)
        and match_field(day, moment.day, 1, 31)
        and match_field(month, moment.month, 1, 12)
        and match_field(weekday, weekday_value, 0, 6)
    )


def match_field(expression: str, value: int, min_value: int, max_value: int) -> bool:
    """判断单个 cron 字段是否命中某个值。"""
    if expression == "*":
        return True

    if expression.startswith("*/"):
        try:
            step = int(expression[2:])
        except ValueError:
            return False
        if step <= 0:
            return False
        # 从字段范围的起点算(day/month 是 1 起,不是 0 起)
        return (value - min_value) % step == 0

    if "," in expression:
        parts = expression.split(",")
        for part in parts:
            try:
                if int(part) == value:
                    return True
            except ValueError:
                return False

        return False

    if "-" in expression:
        parts = expression.split("-")
        if len(parts) != 2:
            return False
        try:
            start = int(parts[0])
            end = int(parts[1])
        except ValueError:
            return False
        return start <= value <= end

    try:
        number = int(expression)
    except ValueError:
        return False
    return number == value


def valid_cron(cron: str) -> bool:
    """校验整个 cron 表达式:5 段、每段可解析且数字在范围内。"""
    fields = cron.split()
    if len(fields) != 5:
        return False
    bounds = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
    return all(
        valid_field(expression, low, high)
        for expression, (low, high) in zip(fields, bounds)
    )


def valid_field(expression: str, low: int, high: int) -> bool:
    """校验单个 cron 字段合法。"""
    if expression == "*":
        return True

    if expression.startswith("*/"):
        try:
            return int(expression[2:]) > 0
        except ValueError:
            return False

    for part in expression.split(","):
        if "-" in part:
            start, _, end = part.partition("-")
            try:
                start_value, end_value = int(start), int(end)
            except ValueError:
                return False
            if not (low <= start_value <= end_value <= high):
                return False
        else:
            try:
                number = int(part)
            except ValueError:
                return False
            if not (low <= number <= high):
                return False
    return True


# 4. 把到期任务放入 cron_queue

def _enqueue_due_job(job: CronJob, minute_marker: str):
    """标记任务已触发,并放进交付队列。"""
    old_pending_delivery = job.pending_delivery
    old_last_fired = job.last_fired

    try:
        job.pending_delivery = True
        job.last_fired = minute_marker
        with cron_queue_lock:
            cron_queue.append(job)
        print(f"[Scheduler] Job {job.id} is due at {minute_marker}")
    except Exception:
        # 入队失败要把标记回滚,否则这个任务再也触发不了
        job.pending_delivery = old_pending_delivery
        job.last_fired = old_last_fired
        raise


# 5. 检查所有任务有没有到时间

def poll_due_jobs(moment: datetime):
    """检查所有任务,把到期的放进交付队列。"""
    minute_marker = moment.strftime("%Y-%m-%d %H:%M")
    with cron_lock:
        for job in list(scheduled_jobs.values()):
            # 情况 1:任务已经触发,但还没有交付给 Agent
            if job.pending_delivery:
                continue
            # 情况 2:当前这一分钟已经触发过
            if job.last_fired == minute_marker:
                continue
            # 情况 3:判断 Cron 表达式是否匹配当前时间
            if cron_matches(job.cron, moment):
                _enqueue_due_job(job, minute_marker)


# 6. 从队列中取出任务

def consume_cron_queue():
    """取出到期任务;已取消的丢弃,周期任务复位,一次性任务删除。"""
    with cron_lock, cron_queue_lock:
        jobs = list(cron_queue)
        cron_queue.clear()
        live = []
        for job in jobs:
            # 已被 cancel 的任务不再交付
            if scheduled_jobs.get(job.id) is not job:
                continue
            if job.recurring:
                job.pending_delivery = False   # 复位,下个周期还能触发
            else:
                # 一次性任务:交付即从内存删除(落盘在交付后由调用方触发)
                del scheduled_jobs[job.id]
            live.append(job)
        return live


def has_due_jobs() -> bool:
    """判断有没有到期未交付的任务。"""
    with cron_queue_lock:
        return bool(cron_queue)


# 7. 持久化(durable)

def _job_definition(job: CronJob) -> dict:
    """取任务的持久化定义:只存定义,不存交付状态。"""
    return {
        "id": job.id,
        "cron": job.cron,
        "prompt": job.prompt,
        "recurring": job.recurring,
    }


def save_durable_jobs():
    """把 durable 任务原子写盘(临时文件 + os.replace)。"""
    with cron_lock:
        data = [_job_definition(job) for job in scheduled_jobs.values() if job.durable]
    tmp_path = SCHEDULE_FILE.with_name(SCHEDULE_FILE.name + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, SCHEDULE_FILE)


def load_durable_jobs():
    """启动时恢复 durable 任务;文件损坏时报告错误,不静默忽略。"""
    global job_counter
    if not SCHEDULE_FILE.exists():
        return
    try:
        data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("expected a list of jobs")
        jobs = []
        for item in data:
            cron = str(item["cron"])
            if not valid_cron(cron):
                raise ValueError(f"invalid cron expression: {cron!r}")
            jobs.append(CronJob(
                id=str(item["id"]), cron=cron, prompt=str(item["prompt"]),
                recurring=bool(item["recurring"]), durable=True,
            ))
    except Exception as error:
        print(f"[Scheduler] Failed to load {SCHEDULE_FILE.name}: {error}")
        return
    with cron_lock:
        for job in jobs:
            scheduled_jobs[job.id] = job
            # 用已有编号初始化计数器,避免重启后生成撞车的 id
            suffix = job.id.removeprefix("cron_")
            if suffix.isdigit():
                job_counter = max(job_counter, int(suffix))
    print(f"[Scheduler] restored {len(jobs)} durable job(s)")


# 8. 任务管理

def schedule_cron(cron: str, prompt: str, recurring: bool = True,
                  durable: bool = False) -> str:
    """新建一个定时任务,返回任务 id;表达式非法时抛错。"""
    if not valid_cron(cron):
        raise ValueError(f"Invalid cron expression: {cron!r}")
    global job_counter
    with cron_lock:
        job_counter += 1
        job_id = f"cron_{job_counter:04d}"
        job = CronJob(
            id=job_id, cron=cron, prompt=prompt,
            recurring=recurring, durable=durable,
        )
        scheduled_jobs[job_id] = job
    if durable:
        save_durable_jobs()
    return job_id


def list_crons() -> str:
    """列出所有定时任务(返回文本,供工具/控制台使用)。"""
    with cron_lock:
        lines = [
            f"{job.id} | {job.cron} | recurring={job.recurring} | "
            f"durable={job.durable} | pending={job.pending_delivery} | {job.prompt}"
            for job in scheduled_jobs.values()
        ]
    return "\n".join(lines) if lines else "No scheduled jobs."


def cancel_cron(job_id: str) -> bool:
    """取消一个定时任务;不存在返回 False。"""
    with cron_lock:
        job = scheduled_jobs.pop(job_id, None)
        if job is None:
            return False
    if job.durable:
        save_durable_jobs()
    return True


# 9. 调度线程

_scheduler_started = False


def start_scheduler():
    """启动调度线程(仅 CLI 调用;导入本模块不会起任何线程)。"""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    load_durable_jobs()
    threading.Thread(target=scheduler_loop, daemon=True).start()


def scheduler_loop():
    """每秒检查一次有没有任务到期,直到进程退出。"""
    print("[Scheduler] started")
    while True:
        now = datetime.now()   # 使用 Agent 进程的本地时间
        poll_due_jobs(now)
        time.sleep(1)
