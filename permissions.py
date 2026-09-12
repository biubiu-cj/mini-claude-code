import re
import threading
from contextlib import contextmanager

from config import WORKDIR
from mcp_client import mcp_policy_for

# 当前线程是否允许交互确认:定时回合跑在后台线程,不能和主终端抢 stdin
_confirm_allowed = threading.local()


def set_interactive_allowed(allowed: bool):
    """标记当前线程是否允许弹出交互确认(默认允许)。"""
    _confirm_allowed.value = allowed


def interactive_allowed() -> bool:
    """当前线程能否向用户弹确认。"""
    return getattr(_confirm_allowed, "value", True)


# 队友的删除沙箱。队友线程没有终端可确认,而"删除意图"一律要确认,
# 结果它连自己 worktree 里跑 Python 生成的 __pycache__ 都清不掉,白烧轮次。
# 这里开一条窄路:沙箱路径由运行时给(队友当前任务的 worktree),命令只要
# 无法跳出这个目录就自动放行。lead 主线程不设沙箱,行为完全不变。
_delete_sandbox = threading.local()

# 命中任意一条就认为命令可能跳出沙箱。刻意保守:宁可多拒,不可放过。
# cmd 的开关是 `/`、根相对路径是 `\`,所以只拦 `\` 开头的 token。
_CAN_ESCAPE = re.compile(
    r"\.\."                      # 上级目录
    r"|[a-zA-Z]:[\\/]"           # 盘符绝对路径
    r"|(?:^|[\s;&|])\\+"         # UNC \\server 或根相对路径 \Windows
    r"|%[^%\s]+%"                # %VAR% 展开
    r"|\$\{?[A-Za-z_]"           # $VAR 展开
)


@contextmanager
def delete_sandbox(path):
    """把当前线程的删除沙箱临时设为 path,退出时恢复原值。"""
    previous = getattr(_delete_sandbox, "path", None)
    _delete_sandbox.path = path
    try:
        yield
    finally:
        _delete_sandbox.path = previous


def sandbox_allows_delete(command: str) -> bool:
    """命令是否是一条跑在沙箱内、且无法逃逸的相对删除。"""
    if getattr(_delete_sandbox, "path", None) is None:
        return False
    if not has_delete_intent(command):
        return False
    return not _CAN_ESCAPE.search(command)


# ---------- 危险命令检测:三级 ----------
# 红:整机 / 整盘级破坏 → 直接拒绝
# 黄:删除文件 → 询问用户
# 绿:其它(if exist / dir / attrib / type / ren…) → 直接放行

# 红名单:只匹配真正毁灭性的命令
CATASTROPHIC_PATTERNS = [
    # rm -rf 只在**目标本身**是根/绝对/盘符/上级/环境变量时才算灾难。
    # 原来用 `.*` 让 `/` 出现在任意位置,于是 `rm -rf ./-p`、`rm -rf ./build`
    # 这种正常的相对清理也被误判成灾难(实测被拦过)。允许前置任意 `-x` 标志,
    # 所以 `rm -rf --no-preserve-root /` 仍然会被拦下。
    r"(?i)\brm\s+(?:-[a-z0-9-]+\s+)*(?:/|~|\.\.|\$|[a-z]:[\\/])",  # rm -rf / 等
    # rd /s 只在**目标是盘根**时才算灾难;`rd /s /q D:\path\dir` 是正常清理目录。
    # 和上面 rm 同理:用 `.*` 会把任何盘符路径都误判成"删盘"(实测拦过 __pycache__ 清理)。
    r"(?i)\b(?:rd|rmdir)\s+(?:/[a-z]\s+)*[a-z]:[\\/]?\s*(?:$|[&|])",  # rd /s C:\
    r"(?i)\bRemove-Item\b[^;&|\n]*-Recurse\b[^;&|\n]*[a-z]:[\\/]",  # 递归删盘
    r"(?i)\bformat\s+[a-z]:",                                # format C:
    r"(?i)\b(?:diskpart|mkfs|shutdown|reboot|stop-computer|restart-computer)\b",
    r"(?i)\bdd\b[^;&|\n]*if=",                               # 直接写盘
]


def is_catastrophic(command: str) -> bool:
    """是不是整机 / 整盘级破坏命令。"""
    return any(re.search(p, command) for p in CATASTROPHIC_PATTERNS)


# 黄:检测"删除行为"——删除动词后必须带操作对象,不会误伤检查类命令
DELETE_INTENT = re.compile(
    r"(?i)\b(?:del|erase|rm|rmdir|rd|Remove-Item)\s+[^ \t;&|()]"   # cmd / ps / unix
    r"|\b(?:os\.remove|os\.unlink|os\.rmdir|shutil\.rmtree)\s*\("  # python
    r"|\.unlink\s*\("
)


def has_delete_intent(command: str) -> bool:
    """命令里有没有删除动作(而不是仅仅提到删这个字)。"""
    return bool(DELETE_INTENT.search(command))


PERMISSION_RULES = [
    {
        "tools": ["read_file", "write_file", "edit_file"],
        "check": lambda args: not (
            WORKDIR / args.get("path", "")
        ).resolve().is_relative_to(WORKDIR),
        "message": "Access outside workspace",
    },
    {
        "tools": ["bash"],
        "check": lambda args: has_delete_intent(args.get("command", "")),
        "message": "This command deletes files/directories",
    },
]


def check_rules(tool_name: str, args: dict) -> str | None:
    """命中黄名单规则就返回原因,否则返回 None。"""
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None


def ask_user(tool_name: str, args: dict, reason: str) -> str:
    """就一次可疑操作询问用户,返回 allow 或 deny。"""
    # 只用 ASCII 符号:Windows 控制台常是 GBK,特殊符号编不出来会直接抛
    # UnicodeEncodeError,让权限确认变成崩溃
    print(f"\n[!]  {reason}")
    print(f"   Tool: {tool_name}({args})")
    choice = input("   Allow? [y/N] ").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"


def report_command(tool_name: str, arguments: dict) -> str:
    """把一次调用压成一行短文本,用于日志里看清被拦下的到底是什么。"""
    if tool_name == "bash":
        return str(arguments.get("command", ""))[:200]
    return f"{tool_name}({arguments})"[:200]


#总校验命令是否合法
def check_permission(tool_call, arguments) -> bool:
    """总闸门:红名单直接拒绝,黄名单问用户,其余放行。"""
    tool_name = tool_call.function.name

    # 红:整机 / 整盘级破坏 → 直接拒绝,不弹窗
    if tool_name == "bash" and is_catastrophic(arguments.get("command", "")):
        print("\n[!]  Catastrophic command blocked")
        print(f"   Blocked command: {report_command(tool_name, arguments)}")
        return False

    # MCP 外部工具:授权只看宿主策略。server 自报的 readOnlyHint 不是授权依据,
    # 策略表里没有的一律要用户确认
    if tool_name.startswith("mcp__"):
        if mcp_policy_for(tool_name) == "allow":
            return True
        reason = f"External MCP tool {tool_name} is not allowlisted"
        if not interactive_allowed():
            print(f"\n[!]  {reason} - auto denied (no interactive approver here)")
            print(f"   Blocked command: {report_command(tool_name, arguments)}")
            return False
        if ask_user(tool_name, arguments, reason) == "deny":
            return False
        return True

    # 黄:删除 / 越界访问等 → 询问用户
    reason = check_rules(tool_name, arguments)
    if reason:
        # 队友在自己的任务 worktree 里做相对删除:没有终端可确认,放行。
        # 逃逸风险由 sandbox_allows_delete 的保守检查兜住;lead 不设沙箱,不受影响。
        if tool_name == "bash" and sandbox_allows_delete(arguments.get("command", "")):
            return True
        if not interactive_allowed():
            # 定时回合、队友线程和无人值守驱动都在后台,不能和主终端抢 input();
            # 这里无条件拒绝,并把命令原文打出来,否则日志里看不出被拦的是哪条
            print(f"\n[!]  {reason} - auto denied (no interactive approver here)")
            print(f"   Blocked command: {report_command(tool_name, arguments)}")
            return False
        decision = ask_user(tool_name, arguments, reason)
        if decision == "deny":
            return False
    return True
