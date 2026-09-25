# owned by cj
"""任务依赖图的纯函数测试:环检测与前置完成判定。"""
import pytest

from taskboard import Task, TaskStore


def make_task(task_id, blocked_by=(), status="pending"):
    """构造一条测试用任务,省得每个用例都写全字段。"""
    return Task(
        id=task_id,
        subject=f"task {task_id}",
        description="",
        status=status,
        blocked_by=list(blocked_by),
    )


@pytest.fixture
def store(tmp_path):
    """指向临时目录的 TaskStore,不碰仓库里的 .tasks。"""
    return TaskStore(tmp_path)


def test_valid_task_id_only_accepts_task_prefix_plus_hex(store):
    """任务 id 必须是 task_ 加 8 位十六进制。"""
    assert store.valid_task_id("task_00000001")
    assert store.valid_task_id("task_deadbeef")
    assert not store.valid_task_id("task_zzzzzzzz")
    assert not store.valid_task_id("task_1234567")
    assert not store.valid_task_id("00000001")


def test_task_cannot_depend_on_itself(store):
    """自依赖即环。"""
    assert store.has_cycle("task_00000001", ["task_00000001"], {})


def test_two_task_cycle_is_detected(store):
    """A 依赖 B、B 又依赖 A,构成环。"""
    tasks = {
        "task_00000001": make_task("task_00000001", ["task_00000002"]),
        "task_00000002": make_task("task_00000002", ["task_00000001"]),
    }
    assert store.has_cycle("task_00000001", ["task_00000002"], tasks)


def test_transitive_cycle_is_detected(store):
    """环要跨多跳也能查出来:C→B→A,再让 A 依赖 C。"""
    tasks = {
        "task_00000001": make_task("task_00000001"),
        "task_00000002": make_task("task_00000002", ["task_00000001"]),
        "task_00000003": make_task("task_00000003", ["task_00000002"]),
    }
    assert store.has_cycle("task_00000001", ["task_00000003"], tasks)


def test_chain_without_cycle_is_allowed(store):
    """正常的前置链不该被判成环。"""
    tasks = {
        "task_00000001": make_task("task_00000001"),
        "task_00000002": make_task("task_00000002", ["task_00000001"]),
        "task_00000003": make_task("task_00000003", ["task_00000002"]),
    }
    assert not store.has_cycle("task_00000003", ["task_00000001"], tasks)


def test_unrelated_existing_cycle_does_not_false_positive(store):
    """别处已有的环不该殃及这条无关的新依赖。"""
    # has_cycle 只看新加的边:新产生的环必然经过其中至少一条。
    # 拿整张图做检测的话,这个用例会误报
    tasks = {
        "task_00000001": make_task("task_00000001", ["task_00000002"]),
        "task_00000002": make_task("task_00000002", ["task_00000001"]),
        "task_00000003": make_task("task_00000003"),
    }
    assert not store.has_cycle("task_00000003", [], tasks)
    assert not store.has_cycle("task_00000003", ["task_00000001"], tasks)


def test_incomplete_dependencies_lists_unfinished_ones(store):
    """只列没完成的前置。"""
    tasks = {
        "task_00000001": make_task("task_00000001", ["task_00000002", "task_00000003"]),
        "task_00000002": make_task("task_00000002", status="completed"),
        "task_00000003": make_task("task_00000003", status="pending"),
    }
    assert store.incomplete_dependencies(tasks["task_00000001"], tasks) == ["task_00000003"]


def test_missing_dependency_counts_as_incomplete(store):
    """前置任务文件不存在时视为未完成,不能让任务被误判为可开始。"""
    tasks = {
        "task_00000001": make_task("task_00000001", ["task_00000009"]),
    }
    assert store.incomplete_dependencies(tasks["task_00000001"], tasks) == ["task_00000009"]


def test_can_start_follows_dependency_status(store):
    """前置全完成才可开始。"""
    tasks = {
        "task_00000001": make_task("task_00000001", ["task_00000002"]),
        "task_00000002": make_task("task_00000002", status="pending"),
    }
    assert not store.can_start("task_00000001", tasks)
    tasks["task_00000002"].status = "completed"
    assert store.can_start("task_00000001", tasks)


def test_task_without_dependencies_can_start(store):
    """没有前置的任务随时可开始。"""
    tasks = {"task_00000001": make_task("task_00000001")}
    assert store.can_start("task_00000001", tasks)
