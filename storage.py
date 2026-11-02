"""日记存储：按人分文件夹、聊天缓冲、索引与状态。

目录结构（桌面）：
    大肥鱼日记（不要删除）/
    ├── 与主人的美好记忆/        ← 主人的日记
    │   ├── 20261102-0013.md
    │   └── index.md
    ├── 小晴/                    ← 亲密朋友的日记，用昵称当文件夹名
    │   └── ...
    └── index.md                 ← 总索引（谁有几个文件夹）
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path

DEFAULT_STATE = {
    "last_write_ts": 0.0,
    "diary_count": 0,
    # 按人记进度：{uid: 上次写进日记的最新消息时间}
    "consumed": {},
    # 按人记指纹：{uid: 上一批素材指纹}
    "fingerprints": {},
    # 按人记文件夹名：{uid: "小晴"}
    "folders": {},
    # 旧版本字段，保留兼容
    "consumed_lines": 0,
    "last_msg_ts": 0.0,
    "last_diary_fingerprint": "",
}

SEP = "---"
OWNER_FOLDER = "与主人的美好记忆"
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def safe_folder_name(name: str, fallback: str = "未命名") -> str:
    """文件夹名不能带路径符号，也不能太长。"""
    n = _ILLEGAL.sub("", str(name or "")).strip().strip(".")
    n = re.sub(r"\s+", " ", n)
    if not n:
        n = fallback
    return n[:40]


class DiaryStore:
    def __init__(self, data_dir: Path, diary_root: Path, owner_folder: str = OWNER_FOLDER) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.diary_dir = Path(diary_root)
        self.diary_dir.mkdir(parents=True, exist_ok=True)
        self.owner_folder = safe_folder_name(owner_folder or OWNER_FOLDER, OWNER_FOLDER)
        self.archive_dir = self.dir / "archive"
        self.chat_path = self.dir / "chats.jsonl"
        self.state_path = self.dir / "state.json"
        self._lock = threading.Lock()
        self.state = self._load_state()

    # ---------------------------------------------------------------- 状态
    def _load_state(self) -> dict:
        data = dict(DEFAULT_STATE)
        data["consumed"] = {}
        data["fingerprints"] = {}
        data["folders"] = {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        except Exception:
            pass
        for key in ("consumed", "fingerprints", "folders"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        return data

    def save(self) -> None:
        with self._lock:
            try:
                tmp = self.state_path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                tmp.replace(self.state_path)
            except Exception:
                pass

    def get(self, key: str, default=None):
        return self.state.get(key, default)

    def set(self, key: str, value) -> None:
        self.state[key] = value
        self.save()

    # ------------------------------------------------------------ 文件夹
    def folder_for(self, uid: str, name: str, is_owner: bool) -> str:
        """这个人的日记放哪个文件夹。主人固定，其他人用昵称。"""
        uid = str(uid or "")
        if is_owner:
            folder = self.owner_folder
        else:
            folder = safe_folder_name(name, f"QQ尾号{uid[-4:]}" if uid else "未命名")
        if uid:
            self.state["folders"][uid] = folder
        return folder

    def folder_of(self, uid: str) -> str:
        """已知这个人的文件夹；没记过就返回空。"""
        return str(self.state["folders"].get(str(uid or "")) or "")

    def list_folders(self) -> list[tuple[str, int]]:
        out = []
        for d in sorted(self.diary_dir.iterdir()):
            if d.is_dir():
                n = len([p for p in d.glob("*.md") if p.name != "index.md"])
                out.append((d.name, n))
        return out

    # ---------------------------------------------------------------- 聊天
    def append_chat(self, umo: str, uid: str, who: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        row = {
            "ts": round(time.time(), 3),
            "umo": umo,
            "uid": str(uid or ""),
            "who": who or "某人",
            "text": text[:600],
        }
        try:
            with self._lock:
                with self.chat_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _read_all_chats(self) -> list[dict]:
        rows: list[dict] = []
        try:
            with self.chat_path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        except FileNotFoundError:
            return []
        except Exception:
            return rows
        return rows

    def chat_line_count(self) -> int:
        return len(self._read_all_chats())

    @staticmethod
    def _row_uid(row: dict) -> str:
        """老数据没有 uid，从 umo 里猜：FriendMessage:12345 → 12345。"""
        uid = str(row.get("uid") or "")
        if uid:
            return uid
        umo = str(row.get("umo") or "")
        tail = umo.rsplit(":", 1)[-1]
        if tail.isdigit() and "Friend" in umo:
            return tail
        return umo or "unknown"

    def pending_by_person(self, limit: int) -> dict[str, list[dict]]:
        """按人分组，返回每人还没写进日记的对话。

        用「这个人上次写进日记的时间」作界，互不干扰。
        """
        consumed = self.state.get("consumed") or {}
        floor_all = float(self.state.get("last_msg_ts") or 0)
        groups: dict[str, list[dict]] = {}
        for row in self._read_all_chats():
            uid = self._row_uid(row)
            floor = float(consumed.get(uid) or 0)
            if not floor and not consumed:
                floor = floor_all  # 老数据迁移：整体游标兜底
            if float(row.get("ts") or 0) <= floor:
                continue
            groups.setdefault(uid, []).append(row)
        if limit:
            for uid in groups:
                groups[uid] = groups[uid][-max(1, int(limit)):]
        return groups

    @staticmethod
    def fingerprint(rows: list[dict]) -> str:
        if not rows:
            return ""
        h = hashlib.sha1()
        for row in rows:
            h.update(f"{row.get('ts')}|{row.get('who')}|{row.get('text')}".encode("utf-8"))
        return h.hexdigest()

    def fingerprint_of(self, uid: str) -> str:
        return str((self.state.get("fingerprints") or {}).get(str(uid)) or "")

    def mark_consumed(self, uid: str, last_ts: float = 0.0, fingerprint: str = "") -> None:
        """记住「这个人的日记写到哪了」。"""
        uid = str(uid or "")
        if not uid:
            return
        consumed = self.state.setdefault("consumed", {})
        if last_ts:
            consumed[uid] = max(float(consumed.get(uid) or 0), float(last_ts))
        if fingerprint:
            self.state.setdefault("fingerprints", {})[uid] = fingerprint
        self.state["last_msg_ts"] = max(
            float(self.state.get("last_msg_ts") or 0), float(last_ts or 0)
        )
        self.save()

    # ---------------------------------------------------------------- 日记
    def save_diary(self, meta: dict, body: str, folder: str) -> Path:
        target = self.diary_dir / safe_folder_name(folder, self.owner_folder)
        target.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M", time.localtime())
        path = target / f"{stamp}.md"
        n = 1
        while path.exists():
            n += 1
            path = target / f"{stamp}-{n}.md"
        head = [
            SEP,
            f"date: {meta.get('date', '')}",
            f"range: {meta.get('range', '')}",
            f"messages: {meta.get('messages', 0)}",
            f"mood: {meta.get('mood', '')}",
            f"title: {meta.get('title', '')}",
            f"tags: {', '.join(meta.get('tags') or [])}",
            f"with: {meta.get('with', '')}",
            SEP,
            "",
            f"# {meta.get('title') or '这一天'}",
            "",
            (body or "").strip(),
            "",
        ]
        path.write_text("\n".join(head), encoding="utf-8")
        self.state["last_write_ts"] = time.time()
        self.state["diary_count"] = int(self.state.get("diary_count") or 0) + 1
        self.save()
        self.rebuild_index(target)
        self.rebuild_master_index()
        return path

    @staticmethod
    def _parse(path: Path) -> dict:
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:
            return {}
        meta: dict = {"path": str(path), "name": path.name, "folder": path.parent.name}
        body = raw
        lines = raw.splitlines()
        if lines and lines[0].strip() == SEP:
            for idx in range(1, len(lines)):
                if lines[idx].strip() == SEP:
                    for line in lines[1:idx]:
                        if ":" in line:
                            key, _, value = line.partition(":")
                            meta[key.strip()] = value.strip()
                    body = "\n".join(lines[idx + 1:])
                    break
        meta["body"] = body.strip()
        meta["tags"] = [
            t.strip() for t in str(meta.get("tags") or "").replace("，", ",").split(",") if t.strip()
        ]
        return meta

    def _folder_diaries(self, folder: str) -> list[dict]:
        d = self.diary_dir / folder
        if not d.is_dir():
            return []
        files = [p for p in d.glob("*.md") if p.name != "index.md"]
        files.sort(key=lambda p: p.name, reverse=True)
        return [self._parse(p) for p in files]

    def list_diaries(self, folder: str | None = None) -> list[dict]:
        """不传 folder 就返回所有人的（按文件名倒序）。"""
        if folder:
            return self._folder_diaries(folder)
        items: list[dict] = []
        for name, _ in self.list_folders():
            items.extend(self._folder_diaries(name))
        items.sort(key=lambda x: str(x.get("name") or ""), reverse=True)
        return items

    def recent(self, n: int, folder: str | None = None) -> list[dict]:
        return self.list_diaries(folder)[: max(0, int(n))]

    def search(self, keyword: str, limit: int = 5, folder: str | None = None) -> list[dict]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        hits = []
        for item in self.list_diaries(folder):
            blob = f"{item.get('title', '')}\n{item.get('body', '')}\n{','.join(item.get('tags') or [])}"
            if keyword in blob:
                hits.append(item)
                if len(hits) >= limit:
                    break
        return hits

    def rebuild_index(self, folder_dir: Path | None = None) -> None:
        """重建某个文件夹的 index.md。"""
        if folder_dir is None:
            for name, _ in self.list_folders():
                self.rebuild_index(self.diary_dir / name)
            return
        folder_dir = Path(folder_dir)
        if not folder_dir.is_dir():
            return
        items = self._folder_diaries(folder_dir.name)
        lines = [f"# {folder_dir.name} · 日记索引", "", f"共 {len(items)} 篇，新的在上面。", ""]
        for item in items:
            title = item.get("title") or item.get("name")
            date = item.get("date") or ""
            tags = " ".join(f"`{t}`" for t in (item.get("tags") or [])[:4])
            lines.append(f"- [{date}｜{title}]({item['name']}) {tags}".rstrip())
        try:
            (folder_dir / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass

    def rebuild_master_index(self) -> None:
        folders = self.list_folders()
        lines = ["# 大肥鱼的日记 · 总索引", "", f"共 {len(folders)} 个文件夹。", ""]
        for name, count in folders:
            lines.append(f"- [{name}]({name}/index.md) —— {count} 篇")
        total = sum(c for _, c in folders)
        lines.insert(3, f"合计 {total} 篇。")
        try:
            (self.diary_dir / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass

    def apply_retention(self, days: int) -> int:
        days = int(days or 0)
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        moved = 0
        for folder, _ in self.list_folders():
            for path in list((self.diary_dir / folder).glob("*.md")):
                if path.name == "index.md":
                    continue
                try:
                    if path.stat().st_mtime < cutoff:
                        dest = self.archive_dir / folder
                        dest.mkdir(parents=True, exist_ok=True)
                        path.replace(dest / path.name)
                        moved += 1
                except Exception:
                    continue
        if moved:
            self.rebuild_index()
            self.rebuild_master_index()
        return moved
