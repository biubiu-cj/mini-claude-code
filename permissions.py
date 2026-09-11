import re
import threading

from config import WORKDIR


# 当前线程是否允许交互确认:定时回合跑在后台线程,不能和主终端抢 stdin
_confirm_allowed = threading.local()


def set_interactive_allowed(allowed: bool):
    """标记当前线程是否允许弹出交互确认(默认允许)。"""
    _confirm_allowed.value = allowed


def interactive_allowed() -> bool:
    """当前线程能否向用户弹确认。"""
    return getattr(_confirm_allowed, "value", True)


# ---------- 危险命令检测:三级 ----------
# 红:整机 / 整盘级破坏 → 直接拒绝
# 黄:删除文件 → 询问用户
# 绿:其它(if exist / dir / attrib / type / ren…) → 直接放行

# 红名单:只匹配真正毁灭性的命令
CATASTROPHIC_PATTERNS = [
    r"(?i)\brm\s+-[a-z]*[rf][a-z]*\b.*(?:/|~|[a-z]:[\\/])",  # rm -rf / 等
    r"(?i)\b(?:rd|rmdir)\s+/s\b.*[a-z]:[\\/]",               # rd /s C:\
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


#总校验命令是否合法
def check_permission(tool_call, arguments) -> bool:
    """总闸门:红名单直接拒绝,黄名单问用户,其余放行。"""
    tool_name = tool_call.function.name

    # 红:整机 / 整盘级破坏 → 直接拒绝,不弹窗
    if tool_name == "bash" and is_catastrophic(arguments.get("command", "")):
        print("\n[!]  Catastrophic command blocked")
        return False

    # 黄:删除 / 越界访问等 → 询问用户
    reason = check_rules(tool_name, arguments)
    if reason:
        if not interactive_allowed():
            # 定时回合在后台线程:拒绝交互确认,避免和主终端抢输入
            print(f"\n[!]  {reason} - auto denied in scheduled turn")
            return False
        decision = ask_user(tool_name, arguments, reason)
        if decision == "deny":
            return False
    return True
