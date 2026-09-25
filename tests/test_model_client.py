# owned by cj
"""模型调用出口测试:分片拼装、429/529 退避、token 计量。

这里全部用**真实 SDK 的 chunk 类型**构造假流,不自己捏造形状 ——
分片拼装是流式改造里最容易错的地方,如果假对象的形状和网关实际下发的不一致,
测试绿了也没意义。工具调用的分片序列是照实测抓下来的:第一片带 id 和 name,
后面的片把 arguments 一个字符一个字符地给。
"""
import threading
from types import SimpleNamespace

import pytest

import model_client
from model_client import (
    RecoveryState,
    USAGE,
    UsageState,
    assemble_stream,
    build_response,
    read_usage,
    retry_kind,
    stream_call_model,
)
from tests.fakes import (
    FakeClient,
    make_chunk,
    sharded_tool_call,
    text_chunk,
    usage_of,
)


@pytest.fixture(autouse=True)
def clean_usage():
    """每个用例前后把会话账清零,用例之间不互相污染。"""
    USAGE.reset()
    yield
    USAGE.reset()


@pytest.fixture
def no_wait(monkeypatch):
    """把指数退避的等待去掉,重试用例不用真的等。"""
    monkeypatch.setattr(model_client, "retry_delay", lambda attempt: 0)


# ---------- 分片拼装 ----------

def test_assemble_concatenates_text_deltas():
    """正文 delta 顺序拼起来就是完整文本。"""
    chunks = [text_chunk("你好"), text_chunk(",世界", finish_reason="stop")]
    content, tool_calls, finish_reason, usage = assemble_stream(chunks)
    assert content == "你好,世界"
    assert tool_calls == []
    assert finish_reason == "stop"
    assert usage is None


def test_assemble_rebuilds_tool_call_sharded_character_by_character():
    """逐字符下发的 arguments 要按 index 拼回完整 JSON。"""
    chunks = sharded_tool_call(0, "call_abc", "read_file", '{"path": "agent.py"}')

    # 先确认这段测试数据真的复现了分片(而不是碰巧只有一个 chunk)
    assert len(chunks) == 1 + len('{"path": "agent.py"}')

    content, tool_calls, finish_reason, usage = assemble_stream(chunks)
    assert content == ""
    assert tool_calls == [{
        "id": "call_abc", "name": "read_file", "arguments": '{"path": "agent.py"}',
    }]


def test_assemble_keeps_parallel_tool_calls_separate():
    """一次返回多个工具调用时,分片按 index 各归各的,不串味。"""
    first = sharded_tool_call(0, "call_a", "read_file", '{"path": "a"}')
    second = sharded_tool_call(1, "call_b", "bash", '{"command": "ls"}')
    # 实测中不同工具调用的分片会交错到达,顺序不保证
    chunks = [second[0], first[0]] + second[1:] + first[1:]

    _, tool_calls, _, _ = assemble_stream(chunks)
    assert [call["id"] for call in tool_calls] == ["call_a", "call_b"]
    assert tool_calls[0]["arguments"] == '{"path": "a"}'
    assert tool_calls[1]["arguments"] == '{"command": "ls"}'


def test_assemble_reports_each_delta_through_the_callback():
    """on_text 收到的正好是每一段 delta,顺序不变。"""
    printed = []
    assemble_stream([text_chunk("流"), text_chunk("式")], printed.append)
    assert printed == ["流", "式"]


def test_assemble_takes_usage_from_an_empty_choices_chunk():
    """OpenAI 官方把 usage 放在最后一个 choices 为空的 chunk 里。"""
    chunks = [
        text_chunk("hi", finish_reason="stop"),
        make_chunk(None, choices=[], usage=usage_of(5, 7)),
    ]
    _, _, _, usage = assemble_stream(chunks)
    assert (usage.prompt_tokens, usage.completion_tokens) == (5, 7)


def test_assemble_takes_usage_from_a_normal_chunk():
    """本项目的 DeepSeek 网关把 usage 挂在最后一个正常 chunk 上,也要收下。"""
    chunks = [text_chunk("hi", finish_reason="stop", usage=usage_of(9, 3))]
    _, _, finish_reason, usage = assemble_stream(chunks)
    assert finish_reason == "stop"
    assert (usage.prompt_tokens, usage.completion_tokens) == (9, 3)


def test_assemble_reports_no_finish_reason_as_none():
    """只有正文、没有结束片的流,finish_reason 保持 None。"""
    _, _, finish_reason, _ = assemble_stream([text_chunk("只说了一半")])
    assert finish_reason is None


# ---------- 响应对象形状 ----------

def test_build_response_keeps_the_original_contract():
    """拼装结果要和 SDK 的响应同形,调用方一行都不用改。"""
    response = build_response(
        "答案",
        [{"id": "call_1", "name": "read_file", "arguments": "{}"}],
        "tool_calls",
        usage_of(1, 2),
    )
    message = response.choices[0].message
    assert message.content == "答案"
    assert message.tool_calls[0].id == "call_1"
    assert message.tool_calls[0].function.name == "read_file"
    assert message.tool_calls[0].function.arguments == "{}"
    assert response.choices[0].finish_reason == "tool_calls"
    assert response.usage.total_tokens == 3


def test_build_response_without_tool_calls_uses_none():
    """没有工具调用时 tool_calls 是 None,和 SDK 的非流式行为一致。"""
    response = build_response("纯文本", [], "stop", None)
    assert response.choices[0].message.tool_calls is None


# ---------- usage 读取与计量 ----------

def test_read_usage_survives_a_missing_usage_object():
    """网关不返回 usage 时按 0 计,不能让 AttributeError 掀翻循环。"""
    assert read_usage(None) == (0, 0)
    assert read_usage(SimpleNamespace(prompt_tokens=None)) == (0, 0)


def test_usage_state_accumulates_and_summarizes():
    """多次调用的用量累加,并且能打印成一行。"""
    state = UsageState()
    state.add(usage_of(100, 50))
    state.add(usage_of(20, 10))
    assert state.snapshot() == (120, 60, 2)
    assert "120 in" in state.summary()
    assert "60 out" in state.summary()
    assert "2 call(s)" in state.summary()


def test_usage_state_counts_a_call_even_without_usage():
    """没有 usage 数据时也记一次调用,只是 token 记 0。"""
    state = UsageState()
    state.add(None)
    assert state.snapshot() == (0, 0, 1)


def test_usage_state_is_thread_safe():
    """并行子 agent 从不同线程记账,一条都不能丢。"""
    state = UsageState()

    def record():
        """记一次 1+1 token 的调用。"""
        for _ in range(100):
            state.add(usage_of(1, 1))

    threads = [threading.Thread(target=record) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert state.snapshot() == (800, 800, 800)
    assert threading.active_count() == 1


# ---------- 错误归类 ----------

@pytest.mark.parametrize("message, expected", [
    ("Error code: 429 - rate limit exceeded", "429"),
    ("RateLimitError", "429"),
    ("Error code: 529 - overloaded", "529"),
    ("The engine is currently overloaded", "529"),
    ("Error code: 400 - invalid request", None),
    ("connection reset by peer", None),
])
def test_retry_kind_classifies_errors(message, expected):
    """只有限流和过载该重试,别的错误直接抛。"""
    assert retry_kind(message) == expected


# ---------- 出口行为 ----------

def test_stream_call_model_asks_for_streaming_with_usage(no_wait):
    """必须同时传 stream 和 include_usage —— 少了后者就拿不到 usage。"""
    fake = FakeClient([[text_chunk("hi", finish_reason="stop")]])
    stream_call_model([{"role": "user", "content": "x"}], None, RecoveryState(),
                      1234, chat_client=fake)

    request = fake.completions.requests[0]
    assert request["stream"] is True
    assert request["stream_options"] == {"include_usage": True}
    assert request["max_tokens"] == 1234


def test_stream_call_model_returns_response_and_records_usage(no_wait):
    """正常一次调用:返回可用的响应对象,并把用量记进会话账。"""
    fake = FakeClient([[text_chunk("hi ", finish_reason="stop",
                                   usage=usage_of(10, 4))]])
    response = stream_call_model([{"role": "user", "content": "x"}], None,
                                 RecoveryState(), 100, chat_client=fake)

    assert response.choices[0].message.content == "hi "
    assert USAGE.snapshot() == (10, 4, 1)


def test_stream_call_model_renders_text_as_it_arrives(no_wait):
    """正文通过 on_text 边收边渲染,而不是等整段结束。"""
    printed = []
    fake = FakeClient([[text_chunk("首"), text_chunk("字"),
                        text_chunk("快", finish_reason="stop")]])
    stream_call_model([{"role": "user", "content": "x"}], None, RecoveryState(),
                      100, on_text=printed.append, chat_client=fake)
    assert printed == ["首", "字", "快"]


def test_stream_call_model_retries_429_then_succeeds(no_wait):
    """撞限流退避重试一次就好,调用方看不出区别。"""
    fake = FakeClient([
        ValueError("Error code: 429 - rate limit exceeded"),
        [text_chunk("ok", finish_reason="stop")],
    ])
    response = stream_call_model([{"role": "user", "content": "x"}], None,
                                 RecoveryState(), 100, chat_client=fake)

    assert len(fake.completions.requests) == 2
    assert response.choices[0].message.content == "ok"


def test_stream_call_model_does_not_retry_other_errors(no_wait):
    """400 之类的错误重试没有意义,立刻抛出去。"""
    fake = FakeClient([ValueError("Error code: 400 - invalid request")])
    with pytest.raises(ValueError, match="400"):
        stream_call_model([{"role": "user", "content": "x"}], None,
                          RecoveryState(), 100, chat_client=fake)
    assert len(fake.completions.requests) == 1


def test_stream_call_model_raises_the_original_error_when_giving_up(no_wait):
    """重试耗尽后抛原始错误,不套一层笼统的 RuntimeError 盖掉原因。"""
    fake = FakeClient([ValueError("Error code: 429 - rate limit exceeded")]
                      * model_client.MAX_RETRIES)
    with pytest.raises(ValueError, match="429"):
        stream_call_model([{"role": "user", "content": "x"}], None,
                          RecoveryState(), 100, chat_client=fake)
    assert len(fake.completions.requests) == model_client.MAX_RETRIES


def test_stream_call_model_switches_model_after_consecutive_overload(no_wait,
                                                                    monkeypatch):
    """连续过载达到阈值就切备用模型,而且只在配了备用模型时才切。"""
    monkeypatch.setattr(model_client, "MAX_CONSECUTIVE_529", 2)
    monkeypatch.setattr(model_client, "FALLBACK_MODEL", "backup-model")
    fake = FakeClient([
        ValueError("Error code: 529 - overloaded"),
        ValueError("Error code: 529 - overloaded"),
        [text_chunk("ok", finish_reason="stop")],
    ])
    stream_call_model([{"role": "user", "content": "x"}], None,
                      RecoveryState(), 100, chat_client=fake)

    models = [request["model"] for request in fake.completions.requests]
    assert models[:2] == [model_client.MODEL, model_client.MODEL]
    assert models[2] == "backup-model"


def test_stream_call_model_does_not_retry_after_printing_text(no_wait):
    """已经吐过正文再断线就不重试了,否则同一段话会打两遍。"""
    printed = []

    def broken_stream():
        """先给一段正文,再抛出可重试的过载错误。"""
        yield text_chunk("已经打出来的半句话")
        raise RuntimeError("Error code: 529 - overloaded")

    fake = FakeClient([broken_stream(), [text_chunk("重复", finish_reason="stop")]])
    with pytest.raises(RuntimeError, match="529"):
        stream_call_model([{"role": "user", "content": "x"}], None,
                          RecoveryState(), 100, on_text=printed.append,
                          chat_client=fake)

    assert len(fake.completions.requests) == 1
    assert "".join(printed) == "已经打出来的半句话"


def test_recovery_state_resets_after_a_successful_call(no_wait):
    """一次成功调用之后 529 计数清零,不会把额度耗在陈旧计数上。"""
    state = RecoveryState(consecutive_529=1)
    fake = FakeClient([[text_chunk("ok", finish_reason="stop")]])
    stream_call_model([{"role": "user", "content": "x"}], None, state, 100,
                      chat_client=fake)
    assert state.consecutive_529 == 0
