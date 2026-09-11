from config import WORKDIR
from permissions import check_permission


#HOOK表
HOOKS = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}

#注册HOOK
def register_hooks(hook_name: str, callback):
    """把一个回调注册到指定事件;事件名不认识就报错。"""
    if hook_name in HOOKS:
        HOOKS[hook_name].append(callback)
    else:
        raise ValueError(f"Unknown hook: {hook_name}")

#启动HOOK
def trigger_hooks(hook_name: str, *args):
    """依次触发事件回调,返回第一个非 None 的结果(即"拦截")。"""
    if hook_name in HOOKS:
        for callback in HOOKS[hook_name]:
            result = callback(*args)
            if result is not None:
                return result
        return None
    else:
        raise ValueError(f"Unknown hook: {hook_name}")

#用户输入词提交前
def context_inject_hook(query: str) -> str | None:
    """在每次提问前打印当前工作目录。"""
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None   # return None = no modification, let prompt through


#工具执行之前
def permission_hook(tool_call, arguments):
    """工具执行前的权限闸门:不放行就返回拒绝原因。"""
    if not check_permission(tool_call, arguments):
        return "Permission denied"
    return None

def log_hook(tool_call, arguments):
    """把每次工具调用打到日志上。"""
    print(f"[HOOK] {tool_call.function.name}(...)")

#判断是否过大
def large_output_hook(tool_call, arguments, output):
    """工具输出超过 10 万字符时提醒一次。"""
    if len(str(output)) > 100000:
        print(f"[HOOK] [!] Large output from {tool_call.function.name}")


#统计对话过程中用了多少次工具
def summary_hook(messages: list) -> None:
    """Agent loop 即将停下时,打印本次会话的工具调用次数。"""
    tool_count = sum(1 for message in messages if message.get("role") == "tool")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None  # return None = no modification, let stop through

_defaults_registered = False


def register_default_hooks():
    """注册全部内置 hook。由入口显式调用;重复调用不会重复注册。"""
    global _defaults_registered
    if _defaults_registered:
        return
    register_hooks("UserPromptSubmit", context_inject_hook)
    register_hooks("PreToolUse", permission_hook)
    register_hooks("PreToolUse", log_hook)
    register_hooks("PostToolUse", large_output_hook)
    register_hooks("Stop", summary_hook)
    _defaults_registered = True
