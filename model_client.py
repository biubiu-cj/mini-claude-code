# owned by cj
"""唯一的模型调用出口:流式接收 + 429/529 退避重试 + token 计量。

改造前仓库里有**两处**直连 `client.chat.completions.create`:lead 主循环
(带全套恢复机制)和 subagent 循环(裸奔,连 max_tokens 都没传)。两套循环
各写一份模型调用,保护措施就必然漏一份 —— 子 agent 撞上限流会把整个子会话掀翻,
而且它烧掉的 token 永远统计不到。

现在 lead 和 subagent 都调这里的 `stream_call_model`,只是传入不同的
`tools` 和 system prompt。三件横切的事(流式 / 重试 / 计量)因此对两者一致生效,
测试时也能通过 `stream_fn` 注入假实现,不联网跑通整条循环。
"""

import threading
import time
from dataclasses import dataclass

from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessage,
    ChatCompletionMessageToolCall,
)
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message_tool_call import Function

from config import (
    DEFAULT_MAX_TOKENS,
    FALLBACK_MODEL,
    MAX_CONSECUTIVE_529,
    MAX_RETRIES,
    MODEL,
    client,
)


def read_usage(usage):
    """从 usage 里安全取出 (prompt, completion) token 数。

    有些兼容 OpenAI 协议的网关不返回 usage(拿到的是 None),直接
    `usage.prompt_tokens` 会 AttributeError 把整个循环掀翻,所以这里一律兜底成 0。
    """
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    return prompt, completion


class UsageState:
    """会话级 token 计量:lead 主循环和所有子 agent 共享这一份账。

    subagent 跑在 ThreadPoolExecutor 的线程里,所以累加必须加锁 ——
    少了这把锁,并行子 agent 的用量会互相覆盖丢掉。
    """

    def __init__(self):
        """从零开始记账。"""
        self._lock = threading.Lock()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0

    def add(self, usage):
        """把一次调用的 usage 累加进会话总量。"""
        prompt, completion = read_usage(usage)
        with self._lock:
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.calls += 1

    def snapshot(self):
        """加锁读出一份 (prompt, completion, calls) 快照。"""
        with self._lock:
            return self.prompt_tokens, self.completion_tokens, self.calls

    def summary(self):
        """一行可打印的统计文本。"""
        prompt, completion, calls = self.snapshot()
        return (
            f"tokens: {prompt:,} in / {completion:,} out "
            f"= {prompt + completion:,} total over {calls} call(s)"
        )

    def reset(self):
        """清零(测试用)。"""
        with self._lock:
            self.prompt_tokens = 0
            self.completion_tokens = 0
            self.calls = 0


# 进程内唯一的一份账。放在入口层(而非 agent_loop 里)是刻意的:
# 账必须覆盖 lead + 所有 subagent,挂在单个循环的局部状态上就漏了子 agent
USAGE = UsageState()


@dataclass
class RecoveryState:
    """一次 agent 轮次内的恢复状态:max_tokens 升级、529 计数、继续写次数。"""
    has_escalated: bool = False
    consecutive_529: int = 0
    recovery_count: int = 0


def retry_delay(attempt):
    """指数退避的等待秒数。"""
    return min(2 ** attempt, 30)


def retry_kind(error_text):
    """把错误文本归类成 `429` / `529`;不该重试的错误返回 None。"""
    text = str(error_text).lower()
    if "429" in text or "rate limit" in text or "ratelimit" in text:
        return "429"
    if "529" in text or "overloaded" in text:
        return "529"
    return None


def assemble_stream(chunks, on_text=None):
    """消费 chunk 迭代器,拼回完整的 (content, tool_calls, finish_reason, usage)。

    这是流式最大的坑:tool_calls 是**分片**拼出来的。第一片给 `id` 和
    `function.name`,后面的片把 `arguments` 字符串**逐字符**追加
    (实测一次调用能拆成十一片)。必须按 `index` 累积,流结束后才是一份完整 JSON。

    usage 的落点各家网关不一致:OpenAI 官方是最后一个 `choices` 为空的 chunk,
    而本项目实测的 DeepSeek 网关把它挂在最后一个**正常** chunk 上。
    所以这里不看 choices 是否为空,只在任何 chunk 上发现 usage 就收下。
    """
    content_parts = []
    fragments = {}
    finish_reason = None
    usage = None
    for chunk in chunks:
        if getattr(chunk, "usage", None) is not None:
            usage = chunk.usage
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        if choice.finish_reason:
            finish_reason = choice.finish_reason
        delta = getattr(choice, "delta", None)
        if delta is None:
            continue
        if getattr(delta, "content", None):
            content_parts.append(delta.content)
            if on_text is not None:
                on_text(delta.content)
        for fragment in getattr(delta, "tool_calls", None) or ():
            slot = fragments.setdefault(
                fragment.index, {"id": None, "name": None, "arguments": []}
            )
            if fragment.id:
                slot["id"] = fragment.id
            function = getattr(fragment, "function", None)
            if function is None:
                continue
            if function.name:
                slot["name"] = function.name
            if function.arguments:
                slot["arguments"].append(function.arguments)
    tool_calls = [
        {
            "id": slot["id"] or f"call_{index}",
            "name": slot["name"] or "",
            "arguments": "".join(slot["arguments"]),
        }
        for index, slot in sorted(fragments.items())
    ]
    return "".join(content_parts), tool_calls, finish_reason, usage


def build_response(content, tool_calls, finish_reason, usage, model=None):
    """把拼装结果包成和 OpenAI SDK 同形的对象。

    契约不变是关键:agent_loop 里的调用方一行都不用改,流式改造就不会
    牵动好几处调用点、也不会漏掉某一处。
    """
    message = ChatCompletionMessage(
        role="assistant",
        content=content or None,
        tool_calls=[
            ChatCompletionMessageToolCall(
                id=call["id"],
                type="function",
                function=Function(name=call["name"], arguments=call["arguments"]),
            )
            for call in tool_calls
        ] or None,
    )
    return ChatCompletion(
        id="chatcmpl-stream",
        choices=[Choice(index=0, finish_reason=finish_reason, message=message)],
        created=int(time.time()),
        # 记实际应答的那个模型(切过备用模型时就不是 MODEL 了)。
        # 没配 MODEL_ID 的场合(比如跑单测)给个占位名,这只是响应元信息
        model=model or MODEL or "unknown",
        object="chat.completion",
        usage=usage,
    )


def stream_call_model(messages, tools, state, max_tokens=DEFAULT_MAX_TOKENS,
                      on_text=None, model=None, chat_client=None):
    """唯一模型调用出口:流式接收、429/529 退避重试、usage 计量,返回完整响应。

    `on_text` 给正文 delta 的实时回调(lead 传打印函数,subagent 传 None)。
    """
    active_model = model or MODEL
    active_client = chat_client or client
    for attempt in range(MAX_RETRIES):
        emitted = False

        def emit(text):
            """记录"已经吐过文本了",同时把 delta 交给调用方的回调。"""
            nonlocal emitted
            emitted = True
            if on_text is not None:
                on_text(text)

        try:
            stream = active_client.chat.completions.create(
                model=active_model, messages=messages, tools=tools,
                tool_choice="auto", max_tokens=max_tokens,
                stream=True,
                # 不带这个开关,部分网关(含 OpenAI 官方)不会在流里给 usage,
                # 成本统计就拿不到数据 —— 这也是"流式和计量必须一起做"的原因
                stream_options={"include_usage": True},
            )
            content, tool_calls, finish_reason, usage = assemble_stream(stream, emit)
        except Exception as error:
            kind = retry_kind(error)
            if kind is None:
                raise
            if kind == "529":
                state.consecutive_529 += 1
                if state.consecutive_529 >= MAX_CONSECUTIVE_529 and FALLBACK_MODEL:
                    active_model = FALLBACK_MODEL
                    state.consecutive_529 = 0
                    print(f"\n[529] switching to {FALLBACK_MODEL}")
            if emitted:
                # 已经往终端吐了一部分正文:重试会把同一段话再打一遍,
                # 比直接失败更难受。这里不再重试,把原始错误抛出去
                raise
            # 重试耗尽就抛出**原始**错误:套一层笼统的 RuntimeError 会把
            # 429 / 529 的区分盖掉,日志里就看不出真实原因
            if attempt == MAX_RETRIES - 1:
                raise
            delay = retry_delay(attempt)
            print(f"\n[{kind}] retry {attempt + 1}/{MAX_RETRIES} in {delay:.0f}s")
            time.sleep(delay)
        else:
            state.consecutive_529 = 0
            USAGE.add(usage)
            return build_response(
                content, tool_calls, finish_reason, usage, active_model
            )
    # 只有 MAX_RETRIES <= 0 才会走到这里
    raise RuntimeError(f"model call failed after {MAX_RETRIES} attempts")
