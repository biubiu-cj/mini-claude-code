# owned by cj
"""循环级测试:把假模型注入 agent_loop,不联网跑通真正容易出错的路径。

这里用 `stream_fn` 注入点驱动**真实的** stream_call_model,只有网络那一层是假的,
所以分片拼装、坏 JSON 容错、max_tokens 升级、压缩就地替换这些逻辑都是真代码在跑。
这些路径以前测不了 —— 循环体内部硬编码了 client。
"""
import functools

import pytest

import agent
import compaction
import hooks
from agent import StreamPrinter, agent_loop
from compaction import COMPACTOR
from config import DEFAULT_MAX_TOKENS, ESCALATED_MAX_TOKENS, MAX_RECOVERY_RETRIES
from memory import MEMORY_MANAGER
from model_client import USAGE, stream_call_model
from todo import TodoManager
from toolschema import SUB_TOOLS
from tests.fakes import FakeClient, streamed, tool_call, usage_of


@pytest.fixture(autouse=True)
def clean_usage():
    """每个用例前后把会话账清零,用例之间不互相污染。"""
    USAGE.reset()
    yield
    USAGE.reset()


@pytest.fixture
def isolated_transcripts(tmp_path, monkeypatch):
    """把 transcript 落盘改到临时目录,别往仓库里写压缩墓碑。"""
    monkeypatch.setattr(compaction, "WORKDIR", tmp_path)
    return tmp_path


def base_messages():
    """一份最小可用的会话:一条 system + 一条 user。"""
    return [
        {"role": "system", "content": "You are a mini agent."},
        {"role": "user", "content": "go"},
    ]


def run_loop(script, messages, todo=None, is_team_turn=True):
    """用脚本化的假客户端跑一轮 agent_loop,返回假客户端以便检查请求。"""
    fake = FakeClient(script)
    stream_fn = functools.partial(stream_call_model, chat_client=fake)
    # is_team_turn=True:跳过早退出时的 memory 提炼,那条路径会真的调模型
    agent_loop(
        messages, todo or TodoManager(), "go", (),
        is_team_turn=is_team_turn, stream_fn=stream_fn,
    )
    return fake


@pytest.fixture
def no_memory(monkeypatch):
    """挡掉 memory 提炼:交互回合收尾时会走那条路,而它真的会调模型。"""
    monkeypatch.setattr(MEMORY_MANAGER, "extract_memories", lambda messages: None)
    monkeypatch.setattr(MEMORY_MANAGER, "consolidate_memories", lambda: None)


def looping_tool_calls(count):
    """造 count 轮"只调工具、不收尾"的响应,用来把循环撑到上限。"""
    return [
        streamed("", [tool_call("todo_write", '{"todos": []}')],
                 finish_reason="tool_calls")
        for _ in range(count)
    ]


def tool_results(messages):
    """取出会话里所有工具结果消息。"""
    return [message for message in messages if message.get("role") == "tool"]


# ---------- 工具调用的分片拼装 ----------

def test_sharded_tool_call_is_assembled_and_executed():
    """逐字符下发的工具调用要能拼成完整 JSON 并真的执行。"""
    messages = base_messages()
    todo = TodoManager()
    run_loop([
        streamed("", [tool_call(
            "todo_write",
            '{"todos": [{"content": "写测试", "status": "in_progress"}]}',
        )], finish_reason="tool_calls"),
        streamed("计划建好了"),
    ], messages, todo)

    assert todo.items == [{"content": "写测试", "status": "in_progress"}]
    assert messages[-1]["content"] == "计划建好了"


def test_two_tool_calls_in_one_turn_both_run():
    """一轮里多个工具调用都要执行,分片不会串味。"""
    messages = base_messages()
    todo = TodoManager()
    run_loop([
        streamed("", [
            tool_call("todo_write", '{"todos": []}', call_id="call_a"),
            tool_call("read_file", '{"path": "config.py", "limit": 1}',
                      call_id="call_b"),
        ], finish_reason="tool_calls"),
        streamed("ok"),
    ], messages, todo)

    results = tool_results(messages)
    assert [result["tool_call_id"] for result in results] == ["call_a", "call_b"]
    # limit=1,所以拿到的是 config.py 的第一行
    assert results[1]["content"] == "import os"


def test_text_and_tool_call_in_the_same_response_are_both_kept():
    """同一次回答里既有正文又有工具调用时两边都不能丢。"""
    messages = base_messages()
    todo = TodoManager()
    run_loop([
        streamed("我先建个计划", [tool_call("todo_write", '{"todos": []}')],
                 finish_reason="tool_calls"),
        streamed("done"),
    ], messages, todo)

    assistant = next(
        message for message in messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert assistant["content"] == "我先建个计划"


# ---------- 坏输入容错 ----------

def test_bad_json_arguments_are_fed_back_to_the_model():
    """工具参数是坏 JSON 时把错误还给模型,而不是掀翻循环。"""
    messages = base_messages()
    todo = TodoManager()
    run_loop([
        streamed("", [tool_call("todo_write", "{这不是合法 JSON")],
                 finish_reason="tool_calls"),
        streamed("我重新调用"),
    ], messages, todo)

    assert "invalid tool arguments JSON" in tool_results(messages)[0]["content"]
    assert todo.items == []
    assert messages[-1]["content"] == "我重新调用"


def test_unknown_tool_is_fed_back_to_the_model():
    """模型臆造工具名时也要能继续跑。"""
    messages = base_messages()
    run_loop([
        streamed("", [tool_call("no_such_tool", "{}")], finish_reason="tool_calls"),
        streamed("换个工具"),
    ], messages)

    assert "unknown tool no_such_tool" in tool_results(messages)[0]["content"]


def test_tool_error_is_fed_back_to_the_model():
    """工具自己抛错时把异常文本还给模型,让它自己纠正。"""
    messages = base_messages()
    run_loop([
        streamed("", [tool_call("read_file", '{"path": "../outside.txt"}')],
                 finish_reason="tool_calls"),
        streamed("读不了,换一个"),
    ], messages)

    result = tool_results(messages)[0]["content"]
    assert result.startswith("Error: ")
    assert "outside WORKDIR" in result


# ---------- max_tokens 升级与继续写 ----------

def test_length_finish_reason_escalates_max_tokens_once():
    """第一次顶到输出上限:丢掉截断输出,用更大的预算重跑。"""
    messages = base_messages()
    fake = run_loop([
        streamed("被截断的半句", finish_reason="length"),
        streamed("完整回答"),
    ], messages)

    budgets = [request["max_tokens"] for request in fake.completions.requests]
    assert budgets == [DEFAULT_MAX_TOKENS, ESCALATED_MAX_TOKENS]
    # 截断的那段不进上下文,最终只留下完整回答
    assert "被截断的半句" not in str(messages)
    assert messages[-1]["content"] == "完整回答"


def test_continuation_stops_after_the_cap():
    """升级后还一直顶上限:继续写有限次就干净收手,不会无限循环。"""
    messages = base_messages()
    fake = run_loop(
        [streamed("半句", finish_reason="length")]
        * (1 + MAX_RECOVERY_RETRIES + 1),
        messages,
    )

    assert len(fake.completions.requests) == 1 + 1 + MAX_RECOVERY_RETRIES
    continuations = [
        message for message in messages
        if message.get("content") == "Continue exactly where you left off."
    ]
    assert len(continuations) == MAX_RECOVERY_RETRIES


# ---------- 压缩后的引用必须回到调用方 ----------

def test_caller_sees_the_compacted_message_list():
    """压缩就地替换 messages:调用方手里的引用必须看到压缩结果。"""
    # 60 条会触发 snip_compact(上限 50)。如果循环里写的是 `messages = prepare(...)`,
    # 调用方这份引用就永远停在压缩前的 60 条,后续新增消息也写不进去
    messages = [{"role": "system", "content": "You are a mini agent."}]
    messages += [{"role": "user", "content": f"m{index}"} for index in range(59)]

    run_loop([streamed("收尾")], messages)

    assert len(messages) == 51
    assert any(
        "messages archived at" in str(message.get("content")) for message in messages
    )
    assert messages[-1]["content"] == "收尾"


def test_reactive_compact_also_replaces_the_caller_list(isolated_transcripts,
                                                        monkeypatch):
    """网关报上下文超限后补救压缩,压缩结果同样要回到调用方。"""
    monkeypatch.setattr(COMPACTOR, "summarize_history", lambda messages: "历史摘要")
    messages = base_messages()
    fake = run_loop([
        ValueError("This model's maximum context length is 128000 tokens"),
        streamed("好了"),
    ], messages)

    assert len(fake.completions.requests) == 2
    assert "历史摘要" in str(messages[1]["content"])
    assert messages[-1]["content"] == "好了"


# ---------- 子 agent 走同一出口 ----------

def test_subagent_uses_the_same_exit_and_its_tokens_are_counted():
    """子 agent 的调用必须经过唯一出口,用量要进同一本会话账。"""
    messages = base_messages()
    fake = run_loop([
        streamed("", [tool_call("subagent", '{"prompt": "看一下 todo.py"}')],
                 finish_reason="tool_calls", usage=usage_of(300, 30)),
        streamed("子 agent 的结论", usage=usage_of(100, 10)),
        streamed("汇总完成", usage=usage_of(200, 20)),
    ], messages)

    assert len(fake.completions.requests) == 3
    # 中间那次是子 agent 的调用:用的是子 agent 工具表,并且不往终端渲染正文
    assert fake.completions.requests[1]["tools"] is SUB_TOOLS
    assert "子 agent 的结论" in tool_results(messages)[0]["content"]
    # 改造前子 agent 走直连,这 110 个 token 根本统计不到
    assert USAGE.snapshot() == (600, 60, 3)
    assert messages[-1]["content"] == "汇总完成"


# ---------- 主循环刹车 ----------

def test_non_interactive_turn_is_capped_by_max_turns(monkeypatch, capsys):
    """非交互回合跑飞了要能自己停下,并说明原因。"""
    monkeypatch.setattr(agent, "MAX_TURNS", 3)
    messages = base_messages()
    fake = run_loop(looping_tool_calls(6), messages, is_team_turn=True)

    assert len(fake.completions.requests) == 3
    assert "[brake] stopped after 3 turns" in capsys.readouterr().out


def test_non_interactive_turn_is_capped_by_deadline(monkeypatch, capsys):
    """墙上时间超限同样要停,一次模型调用都不该发出去。"""
    monkeypatch.setattr(agent, "MAX_SECONDS", -1)
    messages = base_messages()
    fake = run_loop([streamed("这轮不该发生")], messages, is_team_turn=True)

    assert fake.completions.requests == []
    assert "[brake] stopped after -1s" in capsys.readouterr().out


def test_user_turn_is_not_capped(monkeypatch, no_memory):
    """用户手动提问的回合不该被同一个上限误伤。"""
    monkeypatch.setattr(agent, "MAX_TURNS", 3)
    messages = base_messages()
    script = looping_tool_calls(5) + [streamed("收尾了")]
    fake = run_loop(script, messages, is_team_turn=False)

    # 上限是 3,但交互回合不受它约束,6 轮全部跑完
    assert len(fake.completions.requests) == 6
    assert messages[-1]["content"] == "收尾了"


# ---------- Hook 与流式渲染 ----------

def test_stop_hook_can_force_another_turn(monkeypatch):
    """Stop 钩子返回内容就把这一轮打回,再跑一次。"""
    forced = []

    def force_once(messages):
        """第一次收尾时打回,第二次放行。"""
        if not forced:
            forced.append(True)
            return "[goal] 目标还没完成"
        return None

    monkeypatch.setitem(hooks.HOOKS, "Stop", [force_once])
    messages = base_messages()
    fake = run_loop([streamed("第一次收尾"), streamed("第二次收尾")], messages)

    assert len(fake.completions.requests) == 2
    assert any(
        message.get("content") == "[goal] 目标还没完成" for message in messages
    )
    assert messages[-1]["content"] == "第二次收尾"


def test_stream_printer_renders_deltas_and_closes_the_line(capsys):
    """正文逐段打印,整段结束后补一个换行。"""
    printer = StreamPrinter()
    printer("逐")
    printer("字")
    printer.finish()
    assert capsys.readouterr().out == "逐字\n"


def test_stream_printer_stays_silent_without_text(capsys):
    """一个字都没写过就不补换行,免得日志里多出空行。"""
    StreamPrinter().finish()
    assert capsys.readouterr().out == ""
