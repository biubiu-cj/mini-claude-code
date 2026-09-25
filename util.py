import json
import re
import threading


# 当前线程的 agent 标签。队友和 lead 是并发跑的,共用一个 print,
# 日志不带标签就没法归因是哪个 agent 干的。
_agent_label = threading.local()


def set_agent_label(label):
    """设置当前线程的 agent 标签(lead 不设置,默认 main)。"""
    _agent_label.name = label


def agent_label():
    """取当前线程的 agent 标签,未设置时当作 main。"""
    return getattr(_agent_label, "name", "main")


def log_tag():
    """日志前缀,形如 `[alice] `。"""
    return f"[{agent_label()}] "


# 当前线程是否跑在子 agent 循环里。trigger_hooks("Stop") 有两个调用点
# (agent_loop 和 run_subagent),Goal 判断只能落在主循环 ——
# 子 agent 的对话说明不了主目标是否完成。
_subagent = threading.local()


def mark_subagent(inside: bool):
    """标记当前线程是否运行在子 agent 循环里。"""
    _subagent.value = inside


def in_subagent() -> bool:
    """当前线程是否在子 agent 循环里。"""
    return getattr(_subagent, "value", False)


# 解析模型返回的 JSON,兼容被 ```json ... ``` 围栏包裹的情况
def parse_json(text):
    """解析模型返回的 JSON,兼容被 ```json 围栏包裹的情况。"""
    text = str(text).strip()
    fence = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    return json.loads(text)


#消息入库前清洗:去掉无法编码成 UTF-8 的孤立代理字符,
#否则 OpenAI SDK 序列化 messages 时抛 UnicodeEncodeError
def clean_text(s):
    """清洗文本,去掉无法编码成 UTF-8 的孤立代理字符。"""
    return str(s).encode("utf-8", "replace").decode("utf-8")
