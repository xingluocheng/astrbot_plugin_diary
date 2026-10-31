"""日记存储：聊天缓冲、日记文件、索引与状态。"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

DEFAULT_STATE = {
    "last_write_ts": 0.0,
    "consumed_lines": 0,
    "last_msg_ts": 0.0,          # 上次写进日记的最新消息时间；重启后靠它去重
    "last_diary_fingerprint": "",  # 上一批素材的指纹，防止同一批写两遍
    "diary_count": 0,
}

SEP = "---"


class DiaryStore:
    def __init__(self, data_dir: Path, diary_root: Path) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        # 日记本体放在桌面的「大肥鱼日记（不要删除）」里，方便主人直接打开看。
        self.diary_dir = Path(diary_root)
        self.diary_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir = self.dir / "archive"
        self.chat_path = self.dir / "chats.jsonl"
        self.index_path = self.diary_dir / "index.md"
        self.state_path = self.dir / "state.json"
        self._lock = threading.Lock()
        self.state = self._load_state()

    # ---------------------------------------------------------------- 状态
    def _load_state(self) -> dict:
        data = dict(DEFAULT_STATE)
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        except Exception:
            pass
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

    # ---------------------------------------------------------------- 聊天
    def append_chat(self, umo: str, who: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        row = {
            "ts": round(time.time(), 3),
            "umo": umo,
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

    def pending_chats(self, limit: int) -> list[dict]:
        """返回还没被写进日记的对话，最多 limit 条。

        以「上次写进日记的最新消息时间」为界，而不是按行号。
        行号会被外部清空缓冲、文件重写搞乱；时间戳不会。
        """
        rows = self._read_all_chats()
        floor = float(self.state.get("last_msg_ts") or 0)
        if floor:
            fresh = [r for r in rows if float(r.get("ts") or 0) > floor]
        else:
            start = int(self.state.get("consumed_lines") or 0)
            fresh = rows[start:] if start < len(rows) else []
        return fresh[-max(1, limit):] if limit else fresh

    @staticmethod
    def fingerprint(rows: list[dict]) -> str:
        """给一批素材算指纹，用来判断是不是同一批。"""
        if not rows:
            return ""
        h = hashlib.sha1()
        for row in rows:
            h.update(f"{row.get('ts')}|{row.get('who')}|{row.get('text')}".encode("utf-8"))
        return h.hexdigest()

    def mark_consumed(self, total: int, last_ts: float = 0.0) -> None:
        """记下已经写进日记的位置。

        last_ts 用「实际用到的最新一条消息」的时间，
        这样即使中途有新消息进来，也不会被误标成已写。
        """
        self.state["consumed_lines"] = int(total)
        if last_ts:
            self.state["last_msg_ts"] = max(
                float(self.state.get("last_msg_ts") or 0), float(last_ts)
            )
        self.save()

    # ---------------------------------------------------------------- 日记
    def save_diary(self, meta: dict, body: str) -> Path:
        now = time.localtime()
        stamp = time.strftime("%Y%m%d-%H%M", now)
        path = self.diary_dir / f"{stamp}.md"
        head = [
            SEP,
            f"date: {meta.get('date', '')}",
            f"range: {meta.get('range', '')}",
            f"messages: {meta.get('messages', 0)}",
            f"mood: {meta.get('mood', '')}",
            f"title: {meta.get('title', '')}",
            f"tags: {', '.join(meta.get('tags') or [])}",
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
        self.rebuild_index()
        return path

    @staticmethod
    def _parse(path: Path) -> dict:
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:
            return {}
        meta: dict = {"path": str(path), "name": path.name}
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

    def list_diaries(self) -> list[dict]:
        # index.md 也在这个目录里，它不是日记，别混进来。
        files = [p for p in sorted(self.diary_dir.glob("*.md"), reverse=True) if p.name != "index.md"]
        return [self._parse(p) for p in files]

    def recent(self, n: int) -> list[dict]:
        return self.list_diaries()[: max(0, int(n))]

    def search(self, keyword: str, limit: int = 5) -> list[dict]:
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        hits = []
        for item in self.list_diaries():
            blob = f"{item.get('title', '')}\n{item.get('body', '')}\n{','.join(item.get('tags') or [])}"
            if keyword in blob:
                hits.append(item)
                if len(hits) >= limit:
                    break
        return hits

    def rebuild_index(self) -> None:
        items = self.list_diaries()
        lines = ["# 日记索引", "", f"共 {len(items)} 篇，新的在上面。", ""]
        for item in items:
            title = item.get("title") or item.get("name")
            date = item.get("date") or ""
            tags = " ".join(f"`{t}`" for t in (item.get("tags") or [])[:4])
            lines.append(f"- [{date}｜{title}]({item['name']}) {tags}".rstrip())
        try:
            self.index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass

    def apply_retention(self, days: int) -> int:
        days = int(days or 0)
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        moved = 0
        for path in list(self.diary_dir.glob("*.md")):
            try:
                if path.stat().st_mtime < cutoff:
                    self.archive_dir.mkdir(exist_ok=True)
                    path.replace(self.archive_dir / path.name)
                    moved += 1
            except Exception:
                continue
        if moved:
            self.rebuild_index()
        return moved
