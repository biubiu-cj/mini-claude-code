# owned by cj
"""会话持久化测试:增量追加、树状分支回溯、元信息原子写入。"""
import json

import pytest

import session
from session import SessionStore, new_session_id, valid_session_id


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    """把会话目录改到临时目录,不碰仓库里的 .sessions。"""
    monkeypatch.setattr(session, "SESSIONS_DIR", tmp_path)
    return tmp_path


def message(role, content):
    """造一条会话消息。"""
    return {"role": role, "content": content}


def test_session_id_is_stable_and_path_safe():
    """会话 id 是时间戳形态,能挡住路径穿越。"""
    assert valid_session_id(new_session_id())
    assert not valid_session_id("../../etc/passwd")
    assert not valid_session_id("20260925")
    assert not valid_session_id("20260925-204501.jsonl")


def test_store_rejects_traversal_id(sessions_dir):
    """构造存储时就拒绝非法 id,不给写坏任意路径的机会。"""
    with pytest.raises(ValueError):
        SessionStore("../evil")


def test_sync_appends_only_the_new_tail(sessions_dir):
    """第二次 sync 只写新增的那几条,不重写已有记录。"""
    store = SessionStore("20260925-204501")
    messages = [message("user", "a"), message("assistant", "b")]

    assert store.sync(messages) == 2
    first_write = store.path.read_text(encoding="utf-8")

    messages.append(message("user", "c"))
    assert store.sync(messages) == 1

    lines = store.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    # 已写过的字节原样保留(只追加,不改写)
    assert store.path.read_text(encoding="utf-8").startswith(first_write)


def test_sync_of_unchanged_messages_writes_nothing(sessions_dir):
    """没有新消息时一条都不写。"""
    store = SessionStore("20260925-204501")
    messages = [message("user", "a")]
    store.sync(messages)
    assert store.sync(messages) == 0


def test_load_messages_round_trips(sessions_dir):
    """写进去什么,读出来就是什么。"""
    store = SessionStore("20260925-204501")
    messages = [
        message("system", "you are an agent"),
        message("user", "读一下 agent.py"),
        message("assistant", "好"),
    ]
    store.sync(messages)

    assert SessionStore("20260925-204501").load_messages() == messages


def test_records_form_a_parent_child_chain(sessions_dir):
    """每条记录带 id 和 parentId,整份历史是一条可回溯的链。"""
    store = SessionStore("20260925-204501")
    store.sync([message("user", "a"), message("assistant", "b")])

    records = [
        json.loads(line)
        for line in store.path.read_text(encoding="utf-8").splitlines()
    ]
    assert records[0]["parentId"] is None
    assert records[1]["parentId"] == records[0]["id"]
    assert len({record["id"] for record in records}) == 2


def test_compaction_starts_a_new_branch(sessions_dir):
    """历史被压缩改写后另起一条分支,读回来的是分支上的内容。"""
    store = SessionStore("20260925-204501")
    store.sync([message("user", f"m{i}") for i in range(6)])

    # 模拟一次压缩:消息列表被换成了更短的一份
    compacted = [message("system", "summary of everything"), message("user", "继续")]
    store.sync(compacted)

    assert store.load_messages() == compacted
    # 被压缩掉的历史仍留在文件里,没有被删除
    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 8


def test_resume_then_append_continues_the_same_branch(sessions_dir):
    """续跑时接着原分支追加,不是从新分支开头重写。"""
    store = SessionStore("20260925-204501")
    store.sync([message("user", "a",)])

    resumed = SessionStore("20260925-204501")
    messages = resumed.load_messages()
    messages.append(message("assistant", "b"))
    resumed.sync(messages)

    assert SessionStore("20260925-204501").load_messages() == [
        message("user", "a"),
        message("assistant", "b"),
    ]
    assert len(resumed.path.read_text(encoding="utf-8").splitlines()) == 2


def test_truncated_last_line_is_skipped(sessions_dir):
    """崩溃留下的半行记录不该让整次会话读不出来。"""
    store = SessionStore("20260925-204501")
    store.sync([message("user", "a")])
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write('{"id": "m000002", "parentId": "m0')

    assert SessionStore("20260925-204501").load_messages() == [message("user", "a")]


def test_meta_round_trips_todo_and_request(sessions_dir):
    """元信息里的当前请求和计划能原样读回。"""
    store = SessionStore("20260925-204501")
    todo = [{"content": "写测试", "status": "in_progress"}]
    store.save_meta("改一下持久化", todo)

    meta = SessionStore("20260925-204501").load_meta()
    assert meta["active_request"] == "改一下持久化"
    assert meta["todo"] == todo


def test_load_meta_falls_back_when_missing_or_corrupt(sessions_dir):
    """元信息缺失或损坏时退回空值,不让续跑崩掉。"""
    store = SessionStore("20260925-204501")
    assert store.load_meta() == {"active_request": "", "todo": []}

    store.meta_path.write_text("{not json", encoding="utf-8")
    assert SessionStore("20260925-204501").load_meta() == {
        "active_request": "", "todo": [],
    }


def test_load_meta_drops_malformed_todo_items(sessions_dir):
    """计划里混进坏条目时只丢坏的那条。"""
    store = SessionStore("20260925-204501")
    store.meta_path.write_text(
        json.dumps({
            "active_request": "x",
            "todo": [{"content": "ok", "status": "pending"}, {"content": "no status"}],
        }),
        encoding="utf-8",
    )
    assert store.load_meta()["todo"] == [{"content": "ok", "status": "pending"}]


def test_latest_session_id_follows_modification_time(sessions_dir):
    """--continue 取的是最近被写过的那次会话。"""
    first = SessionStore("20260925-204501")
    first.sync([message("user", "old")])
    second = SessionStore("20260926-090000")
    second.sync([message("user", "new")])

    assert session.latest_session_id() == "20260926-090000"
    assert set(session.list_session_ids()) == {
        "20260925-204501", "20260926-090000",
    }


def test_latest_session_id_is_none_when_empty(sessions_dir):
    """一次会话都没跑过时没有可续的会话。"""
    assert session.latest_session_id() is None
