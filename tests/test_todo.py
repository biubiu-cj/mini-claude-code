# owned by cj
"""TodoManager 的纯函数测试:校验、覆盖、渲染。"""
import pytest

from todo import TodoManager


def test_fresh_manager_renders_empty_string():
    """刚建的计划渲染出来是空串,不是 '(no plan)'。"""
    # 断言真值而不是想象值:"\\n".join([]) 就是 ""
    assert TodoManager().render() == ""


def test_render_uses_a_marker_per_status():
    """三种状态各渲染成对应的标记。"""
    manager = TodoManager()
    manager.update([
        {"content": "写测试", "status": "pending"},
        {"content": "改循环", "status": "in_progress"},
        {"content": "读代码", "status": "completed"},
    ])
    assert manager.render() == "[ ] 写测试\n[-] 改循环\n[x] 读代码"


def test_update_overwrites_previous_plan():
    """update 是覆盖而不是追加。"""
    manager = TodoManager()
    manager.update([{"content": "a", "status": "pending"}])
    manager.update([{"content": "b", "status": "completed"}])
    assert manager.items == [{"content": "b", "status": "completed"}]


def test_update_accepts_json_string():
    """模型传字符串形式的 JSON 也要能解析。"""
    manager = TodoManager()
    manager.update('[{"content": "a", "status": "pending"}]')
    assert manager.items == [{"content": "a", "status": "pending"}]


def test_update_drops_unknown_fields():
    """校验后只保留 content / status,多余字段不进状态。"""
    manager = TodoManager()
    manager.update([{"content": "a", "status": "pending", "id": 7}])
    assert manager.items == [{"content": "a", "status": "pending"}]


@pytest.mark.parametrize("bad_input", [
    "not json",
    {"content": "a", "status": "pending"},
    ["plain string"],
    [{"status": "pending"}],
    [{"content": "a"}],
    [{"content": "a", "status": "done"}],
    [{"content": "", "status": "pending"}],
])
def test_update_rejects_bad_input(bad_input):
    """坏输入一律抛 ValueError,让模型自己纠正,而不是污染状态。"""
    manager = TodoManager()
    with pytest.raises(ValueError):
        manager.update(bad_input)


def test_rejected_update_leaves_old_plan_intact():
    """一次失败的 update 不能把已有计划清空。"""
    manager = TodoManager()
    manager.update([{"content": "a", "status": "pending"}])
    with pytest.raises(ValueError):
        manager.update("not json")
    assert manager.items == [{"content": "a", "status": "pending"}]
