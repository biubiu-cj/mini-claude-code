# owned by cj
"""权限判定的纯函数测试:灾难命令识别与删除意图识别。"""
import pytest

from permissions import has_delete_intent, is_catastrophic


@pytest.mark.parametrize("command", [
    "rm -rf /",
    "rm -rf --no-preserve-root /",
    "rm -rf ~/",
    "rm -rf ..",
    "rd /s /q C:\\",
    "rmdir /s /q D:\\",
    "Remove-Item -Recurse -Force D:\\data",
    "format C:",
    "diskpart",
    "shutdown /s /t 0",
    "dd if=/dev/zero of=/dev/sda",
])
def test_catastrophic_commands_are_blocked(command):
    """整机/整盘级破坏命令必须被判为灾难。"""
    assert is_catastrophic(command)


@pytest.mark.parametrize("command", [
    "rm -rf ./build",
    "rm -rf ./-p",
    "rm -rf build/dist",
    "rd /s /q D:\\path\\dir",
    "del build\\a.obj",
    "python -c \"import shutil; shutil.rmtree('build')\"",
])
def test_relative_cleanup_is_not_catastrophic(command):
    """正常清理相对目录不能被误判成灾难。"""
    # 真回归测试:早期用 `.*` 匹配,`/` 出现在任意位置就算灾难,
    # 于是 `rm -rf ./build` 这种日常清理被拦死(实测踩过)
    assert not is_catastrophic(command)


def test_empty_command_is_not_catastrophic():
    """空命令不是灾难。"""
    assert not is_catastrophic("")
    assert not has_delete_intent("")


@pytest.mark.parametrize("command", [
    "del a.txt",
    "erase a.txt",
    "rm -rf ./build",
    "rmdir /s build",
    "Remove-Item a.txt",
    "python -c \"import os; os.remove('a')\"",
    "python -c \"import shutil; shutil.rmtree('a')\"",
    "pathlib.Path('a').unlink()",
])
def test_delete_intent_detects_real_deletions(command):
    """真正的删除动作必须被识别出来。"""
    assert has_delete_intent(command)


@pytest.mark.parametrize("command", [
    "dir del.txt",
    "ls deleted",
    "type delete_notes.md",
    "echo 'form a plan'",
    "npm run format src",
])
def test_mentioning_delete_is_not_delete_intent(command):
    """只是把 del/rm 当词根或普通单词用,不构成删除动作,不该弹确认。"""
    assert not has_delete_intent(command)


def test_bare_rm_with_an_argument_counts_as_delete_intent():
    """记录一个刻意的取舍:`rm <目标>` 一律算删除意图,哪怕只是 grep 里提到。"""
    # 删除检测是"宁可多问一句"的保守启发式:它只要求删除动词后面
    # 跟了操作对象,不做语义理解。所以 `grep rm README.md` 也会被算成删除意图。
    # 这个误报的代价是弹一次确认,而漏报的代价是静默删掉用户的东西
    assert has_delete_intent("grep rm README.md")
