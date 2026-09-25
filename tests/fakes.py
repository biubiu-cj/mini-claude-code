# owned by cj
"""测试用的假模型层:构造真实形状的流式 chunk,以及脚本化的假客户端。

分片拼装是最容易写错的地方,所以这里一律用 **SDK 真实的 chunk 类型**,
不自定义形状 —— 假对象和网关实际下发的不一致的话,测试绿了也没意义。
工具调用的分片序列照实测抓下来:第一片带 id 和 name,
后面的片把 arguments 一个字符一个字符地给。
"""
from types import SimpleNamespace

from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.completion_usage import CompletionUsage


def make_chunk(delta, finish_reason=None, usage=None, choices=None):
    """造一个流式 chunk;显式传 choices=[] 可模拟"只带 usage"的收尾片。"""
    if choices is None:
        choices = [Choice(index=0, delta=delta, finish_reason=finish_reason)]
    return ChatCompletionChunk(
        id="chunk", created=0, model="test-model",
        object="chat.completion.chunk", choices=choices, usage=usage,
    )


def text_chunk(text, finish_reason=None, usage=None):
    """造一个只带正文 delta 的 chunk。"""
    return make_chunk(ChoiceDelta(content=text), finish_reason, usage)


def tool_fragment(index, call_id=None, name=None, arguments=None):
    """造一个工具调用分片 chunk。"""
    return make_chunk(ChoiceDelta(tool_calls=[ChoiceDeltaToolCall(
        index=index, id=call_id, type="function" if call_id else None,
        function=ChoiceDeltaToolCallFunction(name=name, arguments=arguments),
    )]))


def sharded_tool_call(index, call_id, name, arguments):
    """按实测形状把一个完整工具调用拆成分片序列。"""
    fragments = [tool_fragment(index, call_id=call_id, name=name, arguments="")]
    fragments += [tool_fragment(index, arguments=character) for character in arguments]
    return fragments


def usage_of(prompt, completion):
    """造一个 usage 对象。"""
    return CompletionUsage(
        prompt_tokens=prompt, completion_tokens=completion,
        total_tokens=prompt + completion,
    )


def streamed(content, tool_calls=(), finish_reason="stop", usage=None):
    """把一次完整回答拆成 chunk 列表,模拟真实网关的下发方式。"""
    chunks = [text_chunk(character) for character in content]
    for index, call in enumerate(tool_calls):
        chunks += sharded_tool_call(
            index, call["id"], call["name"], call["arguments"]
        )
    chunks.append(text_chunk("", finish_reason=finish_reason))
    if usage is not None:
        chunks.append(make_chunk(None, choices=[], usage=usage))
    return chunks


def tool_call(name, arguments, call_id="call_1"):
    """构造一条工具调用(参数已是拼好的 JSON 字符串)。"""
    return {"id": call_id, "name": name, "arguments": arguments}


class FakeCompletions:
    """假的 completions 资源:按脚本依次返回流,或抛出脚本里的异常。"""

    def __init__(self, script):
        """把脚本站住,并记下每次请求的参数。"""
        self.script = list(script)
        self.requests = []

    def create(self, **kwargs):
        """消费脚本里的下一项。"""
        self.requests.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    """假的 OpenAI 客户端,形状与真实 SDK 相同(chat.completions.create)。"""

    def __init__(self, script):
        """把脚本包装成 chat.completions 的形状。"""
        self.completions = FakeCompletions(script)
        self.chat = SimpleNamespace(completions=self.completions)
