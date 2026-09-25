# owned by cj
"""会话持久化:只追加的树状 JSONL,让进程重启后能接着上一次跑。

一次可续跑的会话 = messages + todo 计划 + 当前请求。任务板本来就单独落盘
(.tasks),不属于这里。

存储格式:每行一个记录,记录里带 `id` / `parentId`,所以整份历史是一棵**树**
而不是一条直线 —— 从任意一条记录沿 parentId 回溯都能还原出一条完整分支。
压缩重写历史时不再改动已写的字节,而是从压缩后的快照另起一条分支,
读的时候取最后一条记录所属的那条链即可。
"""

import json
import os
import re
import time
from pathlib import Path

from config import BASE_DIR

SESSIONS_DIR = BASE_DIR / ".sessions"


def new_session_id():
    """按本地时间生成会话 id,形如 20260925-204501。"""
    return time.strftime("%Y%m%d-%H%M%S")


def valid_session_id(session_id):
    """会话 id 只接受时间戳形态,--resume 的输入直接用它挡住路径穿越。"""
    return bool(re.fullmatch(r"\d{8}-\d{6}", str(session_id)))


class SessionStore:
    """一次会话的存储:每条消息一个记录,只追加、不覆盖已写的字节。"""

    def __init__(self, session_id):
        """绑定会话 id 与落盘路径,并读入已有记录。"""
        if not valid_session_id(session_id):
            raise ValueError(f"Invalid session id: {session_id}")
        self.session_id = session_id
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self.path = SESSIONS_DIR / f"{session_id}.jsonl"
        self.meta_path = SESSIONS_DIR / f"{session_id}.meta.json"
        self._records = self._read_records()
        self._next_number = self._max_record_number() + 1
        # 与 messages 一一对应的已落盘记录 id;sync 靠它算出"哪些还是新的"
        self._chain = []

    def _read_records(self):
        """读出所有能解析的记录;崩溃时写了一半的最后一行直接跳过。"""
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # 只追加写的代价:崩在写最后一行的中途会留下半行。
                # 前面的记录都完整,丢掉这半行即可
                continue
        return records

    def _max_record_number(self):
        """现有记录里最大的编号,续跑时接着往下编号。"""
        numbers = []
        for record in self._records:
            match = re.fullmatch(r"m(\d+)", str(record.get("id", "")))
            if match:
                numbers.append(int(match.group(1)))
        return max(numbers, default=0)

    def load_messages(self):
        """从最后一条记录沿 parentId 回溯到链根,还原出消息列表。"""
        by_id = {record["id"]: record for record in self._records}
        current = self._records[-1]["id"] if self._records else None
        branch = []
        while current is not None:
            record = by_id.get(current)
            if record is None:
                break
            branch.append(record)
            current = record.get("parentId")
        branch.reverse()
        self._chain = [record["id"] for record in branch]
        return [record["message"] for record in branch]

    def sync(self, messages):
        """把 messages 里还没落盘的部分追加成新记录,返回追加条数。"""
        if len(messages) >= len(self._chain):
            # 常规路径:历史只增不改,只写多出来的尾巴
            start = len(self._chain)
            parent = self._chain[-1] if self._chain else None
        else:
            # 历史被压缩重写过(变短了):旧链作废,从当前快照另起一条分支。
            # 这也正是"树而不是直线"的用处 —— 被压缩掉的历史仍在文件里,可回溯
            self._chain = []
            start = 0
            parent = None
        records = []
        for message in messages[start:]:
            record_id = f"m{self._next_number:06d}"
            self._next_number += 1
            records.append({"id": record_id, "parentId": parent, "message": message})
            parent = record_id
        if not records:
            return 0
        # 只追加写:天然崩溃安全(最坏也只是最后一行残缺,读的时候跳过),
        # 不需要"临时文件 + os.replace"那一套
        with self.path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
        # 缓存跟着追加一起更新,否则同一个实例 sync 之后再 load_messages
        # 会读到落后于磁盘的旧快照
        self._records.extend(records)
        self._chain.extend(record["id"] for record in records)
        return len(records)

    def save_meta(self, active_request, todo_items):
        """原子写入会话元信息:当前请求与待办计划。"""
        payload = {
            "active_request": active_request,
            "todo": todo_items,
            "updated_at": time.time(),
        }
        temporary = self.meta_path.with_name(self.meta_path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.meta_path)

    def load_meta(self):
        """读回会话元信息;文件缺失或损坏时退回空值,不让续跑崩掉。"""
        empty = {"active_request": "", "todo": []}
        if not self.meta_path.exists():
            return empty
        try:
            payload = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return empty
        return {
            "active_request": str(payload.get("active_request", "")),
            "todo": [
                item for item in payload.get("todo", [])
                if isinstance(item, dict) and "content" in item and "status" in item
            ],
        }


def list_session_ids():
    """按最近修改倒序列出全部会话 id。"""
    if not SESSIONS_DIR.exists():
        return []
    paths = sorted(
        SESSIONS_DIR.glob("*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return [path.stem for path in paths]


def latest_session_id():
    """最近一次会话的 id;一个会话都没有时返回 None。"""
    session_ids = list_session_ids()
    return session_ids[0] if session_ids else None
