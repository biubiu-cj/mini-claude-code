# owned by cj
"""上下文压缩的纯函数测试:体积估算、截断、消费判定、微压缩。"""
import pathlib

import pytest

import compaction
from compaction import COMPACTOR


@pytest.fixture
def isolated_workdir(tmp_path, monkeypatch):
    """把压缩器的落盘目录改到临时目录,别污染仓库。"""
    monkeypatch.setattr(compaction, "WORKDIR", tmp_path)
    return tmp_path


def text_message(role, content):
    """造一条普通消息。"""
    return {"role": role, "content": content}


def tool_call_message(tool_call_id):
    """造一条带工具调用的 assistant 消息。"""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": tool_call_id,
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }],
    }


def tool_result_message(tool_call_id, content):
    """造一条工具结果消息。"""
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def test_estimate_chars_counts_serialized_length():
    """估算值就是序列化后的字符数(含 JSON 数组的方括号)。"""
    messages = [{"role": "user", "content": "hi"}]
    assert COMPACTOR.estimate_chars(messages) == len(
        '[{"role": "user", "content": "hi"}]'
    )


def test_estimate_chars_handles_non_serializable_content():
    """内容里有非 JSON 类型时不能抛异常(default=str 兜住)。"""
    assert COMPACTOR.estimate_chars([{"role": "tool", "content": object()}]) > 0


def test_snip_compact_is_noop_below_limit():
    """消息条数没超上限就原样返回。"""
    messages = [text_message("user", str(i)) for i in range(10)]
    assert COMPACTOR.snip_compact(messages, max_messages=50) == messages


def test_snip_compact_keeps_head_and_tail(isolated_workdir):
    """超长时掐掉中间,留下开头 3 条、结尾若干条和一条归档标记。"""
    messages = [text_message("user", f"m{i}") for i in range(60)]
    result = COMPACTOR.snip_compact(messages, max_messages=50)

    assert len(result) == 50
    assert result[:3] == messages[:3]
    assert result[-46:] == messages[-46:]
    assert "messages archived at" in result[3]["content"]
    assert result[0] is messages[0]


def assert_no_orphan_tool_results(messages):
    """断言结果里没有和工具调用失散的 tool 结果。"""
    declared = set()
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                declared.add(call["id"])
        elif message.get("role") == "tool":
            assert message["tool_call_id"] in declared, "orphan tool result"


def test_snip_compact_never_splits_pair_at_head_cut(isolated_workdir):
    """切点落在头部时,不能把工具调用和它的结果拆开。"""
    messages = [
        text_message("user", "m0"),
        text_message("user", "m1"),
        tool_call_message("call_1"),
    ]
    messages += [tool_result_message("call_1", f"r{i}") for i in range(3)]
    messages += [text_message("user", f"t{i}") for i in range(60)]

    result = COMPACTOR.snip_compact(messages, max_messages=50)

    # head_end 从 3 往后挪,直到跨过那 3 条 tool 结果(被归档的因此少 3 条)
    assert result[2]["tool_calls"] == messages[2]["tool_calls"]
    assert [message["role"] for message in result[3:6]] == ["tool", "tool", "tool"]
    assert result[6]["content"].startswith("[14 messages archived at")
    assert_no_orphan_tool_results(result)


def test_snip_compact_never_splits_pair_at_tail_cut(isolated_workdir):
    """切点落在尾部时,要把前面那条工具调用一起带回来。"""
    # 消息总数 50 时 tail_start 恰好是 4,正好落在 tool 结果上
    messages = [
        text_message("user", "m0"),
        text_message("user", "m1"),
        text_message("user", "m2"),
        tool_call_message("call_1"),
        tool_result_message("call_1", "r0"),
    ]
    messages += [text_message("user", f"t{i}") for i in range(45)]

    result = COMPACTOR.snip_compact(messages, max_messages=50)

    assert result[3]["tool_calls"] == messages[3]["tool_calls"]
    assert result[4]["tool_call_id"] == "call_1"
    assert_no_orphan_tool_results(result)


def test_unseen_tool_result_positions_marks_only_trailing_results():
    """只有后面再没有 assistant 的 tool 结果才算"模型还没消费"。"""
    messages = [
        text_message("user", "go"),
        tool_call_message("call_1"),
        tool_result_message("call_1", "consumed"),
        tool_call_message("call_2"),
        tool_result_message("call_2", "unseen"),
    ]
    unseen = COMPACTOR.unseen_tool_result_positions(messages)
    assert unseen == {(4, "call_2")}


def test_tool_result_budget_persists_oversized_result(isolated_workdir, monkeypatch):
    """超长工具结果落盘,只留一段预览。"""
    monkeypatch.setattr(COMPACTOR, "MAX_CHARS", 100)
    monkeypatch.setattr(COMPACTOR, "LARGE_RESULT_CHAR_LIMIT", 50)
    messages = [text_message("user", "x"), tool_result_message("call_1", "A" * 5000)]

    result = COMPACTOR.tool_result_budget(messages)

    content = result[1]["content"]
    assert content.startswith("[Large tool output saved to")
    assert "Preview:" in content
    # 预览只有前 2000 字,后面的正文已经不在上下文里了
    assert "A" * 2500 not in content
    saved = isolated_workdir / ".task_outputs" / "tool-results" / "call_1.txt"
    assert saved.read_text(encoding="utf-8") == "A" * 5000


def test_tool_result_budget_leaves_small_results_alone(isolated_workdir):
    """总体没超限时一个都不动。"""
    messages = [tool_result_message("call_1", "small")]
    assert COMPACTOR.tool_result_budget(messages)[0]["content"] == "small"


def test_micro_compact_only_touches_old_consumed_results(isolated_workdir, monkeypatch):
    """微压缩只压"已消费且够旧"的结果:最近几条和未消费的要保留。"""
    monkeypatch.setattr(COMPACTOR, "KEEP_RECENT_RESULTS", 3)
    messages = [text_message("user", "go")]
    for index in range(5):
        tool_call_id = f"call_{index}"
        messages.append(tool_call_message(tool_call_id))
        content = f"result {index} " + "B" * 300
        messages.append(tool_result_message(tool_call_id, content))
        if index < 4:
            # 每个结果后面都跟一条 assistant,表示模型已消费
            messages.append(tool_call_message(f"call_{index + 1}"))

    COMPACTOR.micro_compact(messages, target_chars=10)

    contents = {
        message["tool_call_id"]: message["content"]
        for message in messages if message.get("role") == "tool"
    }
    assert contents["call_0"].startswith("[Earlier tool result saved at")
    # 最近 3 条已消费的不动
    assert contents["call_1"].startswith("result 1")
    assert contents["call_2"].startswith("result 2")
    assert contents["call_3"].startswith("result 3")
    # 还没被消费的最后一条不动
    assert contents["call_4"].startswith("result 4")


def test_write_transcript_returns_a_real_file(isolated_workdir):
    """transcript 落盘并返回路径。"""
    path = COMPACTOR.write_transcript([text_message("user", "hello")])
    assert pathlib.Path(path).exists()
    assert "hello" in pathlib.Path(path).read_text(encoding="utf-8")
