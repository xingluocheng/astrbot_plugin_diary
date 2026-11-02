"""大肥鱼的日记 —— 定时把发生的事写成日记，并在聊天时想起来。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools

from . import writer
from .storage import DiaryStore

PLUGIN_NAME = "astrbot_plugin_diary"

# 桌面的日记文件夹。名字带「（不要删除）」，是给主人看的。
DIARY_FOLDER_NAME = "大肥鱼日记（不要删除）"


def resolve_desktop() -> Path:
    """找到真实的桌面目录（可能是 OneDrive 重定向或本地化名称）。"""
    candidates: list[Path] = []
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            raw, _ = winreg.QueryValueEx(key, "Desktop")
        candidates.append(Path(os.path.expandvars(str(raw))))
    except Exception:
        pass

    home = Path.home()
    candidates.extend([home / "Desktop", home / "OneDrive" / "Desktop", home / "桌面"])
    for path in candidates:
        try:
            if path.is_dir():
                return path
        except Exception:
            continue
    return home


def ensure_diary_folder() -> Path:
    """每次启动都检查一次：没有就建。"""
    folder = resolve_desktop() / DIARY_FOLDER_NAME
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning(f"[diary] 桌面日记文件夹建不出来（{exc}），先退回数据目录")
        folder = Path(StarTools.get_data_dir(PLUGIN_NAME)) / "diary"
        folder.mkdir(parents=True, exist_ok=True)
    return folder


DEFAULTS = {
    "enabled": True,
    "interval_hours": 24,
    "min_messages": 30,
    "max_messages_per_diary": 120,
    "inject_enabled": True,
    "inject_recent": 2,
    "inject_max_chars": 1500,
    "owner_ids": "",
    "bot_ids": "",
    "private_only": True,
    "include_intimate": True,
    "intimate_days": 14,
    "intimacy_data": "",
    "startup_write": True,
    "startup_messages": 40,
    "retention_days": 0,
    "persona_extra": "",
    "diary_voice": "第一人称，像睡前随手写在备忘录里",
    "ignore_commands": True,
    "record_self": True,
    "owner_folder": "与主人的美好记忆",
}

# 这些是机器人指令，不该进日记素材
COMMAND_WORDS = {
    "reset", "help", "anysearch", "亲密度", "好感度", "帮助",
    "空间", "空间帮助", "空间状态", "空间测试", "空间发",
    "日记", "日记帮助", "日记列表", "日记状态", "日记写",
}
COMMAND_PREFIXES = ("空间", "日记", "亲密度", "好感度")

# 这些是报错提示，不是她说过的话，不能进日记素材
ERROR_MARKERS = (
    "LLM 响应错误",
    "All chat models failed",
    "APIStatusError",
    "Insufficient Balance",
    "请求失败",
    "模型调用失败",
)

CHECK_INTERVAL = 300  # 后台每 5 分钟看一次是否该写


class DiaryStar(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.plugin_data_dir = data_dir.parent
        self.diary_root = ensure_diary_folder()
        logger.info(f"[diary] 日记目录：{self.diary_root}")
        self.store = DiaryStore(
            data_dir, self.diary_root, owner_folder=str(self._cfg("owner_folder", "") or "")
        )
        self._lock = asyncio.Lock()
        self._loop_task: asyncio.Task | None = None
        self._startup_task: asyncio.Task | None = None
        self._intimacy_cache: tuple[float, dict] = (0.0, {})
        # 启动即补写：等模型和平台加载完，再用最近 N 条写一篇
        try:
            self._startup_task = asyncio.create_task(self._startup_write())
        except Exception as exc:
            logger.debug(f"[diary] 启动补写任务没起来：{exc}")

    # ------------------------------------------------------------- 基础
    def _is_owner(self, event: AstrMessageEvent) -> bool:
        raw = str(self._cfg("owner_ids", "") or "")
        ids = {w.strip() for w in raw.replace("，", ",").split(",") if w.strip()}
        sender = str(event.get_sender_id() or "")
        if ids:
            return sender in ids
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _intimacy_file(self) -> Path:
        override = str(self._cfg("intimacy_data", "") or "").strip()
        if override:
            return Path(override)
        return self.plugin_data_dir / "astrbot_plugin_intimacy" / "users.json"

    def _intimate_days(self, uid: str) -> int:
        """读亲密度插件的数据，返回这个人和她聊过多少天。"""
        try:
            path = self._intimacy_file()
            mtime = path.stat().st_mtime
        except Exception:
            return 0
        cached_mtime, cached = self._intimacy_cache
        if mtime != cached_mtime:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                cached = data if isinstance(data, dict) else {}
            except Exception:
                cached = {}
            self._intimacy_cache = (mtime, cached)
        rec = cached.get(str(uid)) or {}
        days = rec.get("days")
        return len(days) if isinstance(days, list) else 0

    def _is_command_noise(self, text: str) -> bool:
        """指令消息不该进日记。"""
        if not bool(self._cfg("ignore_commands", True)):
            return False
        t = (text or "").strip().lstrip("/").strip()
        if not t:
            return True
        first = t.split()[0]
        if first in COMMAND_WORDS:
            return True
        extra = str(self._cfg("ignore_command_words", "") or "")
        for word in extra.replace("，", ",").split(","):
            word = word.strip()
            if word and first == word:
                return True
        return False

    def _should_record(self, event: AstrMessageEvent) -> bool:
        """记主人的对话；关系够亲密的人也算。"""
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = False
        if bool(self._cfg("private_only", True)) and not is_private:
            return False

        # 大肥鱼自己发的消息不进缓冲（她的话由 on_decorating_result 记，避免重复）
        sender = str(event.get_sender_id() or "")
        bots = {w.strip() for w in str(self._cfg("bot_ids", "") or "").replace("，", ",").split(",") if w.strip()}
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = ""
        if sender and (sender in bots or (self_id and sender == self_id)):
            return False

        if self._is_owner(event):
            return True

        if bool(self._cfg("include_intimate", True)):
            uid = str(event.get_sender_id() or "")
            if uid and self._intimate_days(uid) >= int(self._cfg("intimate_days", 14) or 14):
                return True
        return False

    def _cfg(self, key: str, default=None):
        fallback = DEFAULTS.get(key, default)
        try:
            value = self.config.get(key, fallback)
        except Exception:
            return fallback
        if value is None:
            return fallback
        if isinstance(value, str) and value == "" and isinstance(fallback, str) and fallback:
            return fallback
        return value

    def _provider(self):
        try:
            return self.context.get_using_provider()
        except Exception as exc:
            logger.warning(f"[diary] 取模型失败：{exc}")
            return None

    # ------------------------------------------------------------- 后台循环
    def _ensure_loop(self) -> None:
        if self._loop_task is not None and not self._loop_task.done():
            return
        try:
            self._loop_task = asyncio.create_task(self._loop())
        except Exception as exc:
            logger.debug(f"[diary] 启动后台任务失败：{exc}")

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(CHECK_INTERVAL)
                await self.maybe_write()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug(f"[diary] 后台检查异常：{exc}")

    async def terminate(self):
        for task in (self._loop_task, self._startup_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except Exception:
                    pass

    async def _startup_write(self) -> None:
        """AstrBot 启动后，给攒够的人各补写一篇。"""
        await asyncio.sleep(15)  # 等模型/平台适配器加载完
        if not bool(self._cfg("startup_write", True)):
            return
        limit = int(self._cfg("startup_messages", 40) or 40)
        for attempt in range(3):
            result = await self.maybe_write(force=True, limit=limit)
            if result.get("ok"):
                logger.info(f"[diary] 启动补写完成：{result.get('detail')}")
                return
            logger.debug(f"[diary] 启动补写第 {attempt + 1} 次未成：{result.get('detail')}")
            if result.get("detail") in ("没有攒够的人", "没有新的对话"):
                logger.info(f"[diary] 启动检查：{result.get('detail')}")
                return
            await asyncio.sleep(20)
        logger.info("[diary] 启动补写放弃，等后台定时再写")

    # ------------------------------------------------------------- 写日记
    def _intimacy_record(self, uid: str) -> dict:
        """取亲密度插件里这个人的记录。"""
        try:
            path = self._intimacy_file()
            mtime = path.stat().st_mtime
        except Exception:
            return {}
        cached_mtime, cached = self._intimacy_cache
        if mtime != cached_mtime:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                cached = data if isinstance(data, dict) else {}
            except Exception:
                cached = {}
            self._intimacy_cache = (mtime, cached)
        return cached.get(str(uid)) or {}

    def _person_name(self, uid: str, rows: list[dict]) -> str:
        """这个人的显示名：优先亲密度插件里的昵称，其次聊天缓冲里的 who。"""
        rec = self._intimacy_record(uid)
        name = str(rec.get("name") or "").strip()
        if name and name != str(uid):
            return name
        for row in reversed(rows):
            who = str(row.get("who") or "").strip()
            if who and who not in ("大肥鱼", "主人", "某人", str(uid)):
                return who
        return f"QQ尾号{str(uid)[-4:]}" if uid else "未命名"

    def _can_write_for(self, uid: str) -> bool:
        """主人永远可以；其他人要亲密度够。"""
        if uid in self._owner_ids():
            return True
        if not bool(self._cfg("include_intimate", True)):
            return False
        need = int(self._cfg("intimate_days", 14) or 14)
        return self._intimate_days(uid) >= need

    def _owner_ids(self) -> set[str]:
        raw = str(self._cfg("owner_ids", "") or "")
        return {w.strip() for w in raw.replace("，", ",").split(",") if w.strip()}

    async def maybe_write(self, force: bool = False, limit: int | None = None) -> dict:
        if not bool(self._cfg("enabled", True)):
            return {"ok": False, "detail": "总开关关着"}
        if self._lock.locked():
            return {"ok": False, "detail": "正在写上一篇"}

        interval = int(self._cfg("interval_hours", 24) or 24) * 3600
        last = float(self.store.get("last_write_ts") or 0)
        if not force and last and (time.time() - last) < interval:
            return {"ok": False, "detail": "还没到时间"}

        limit = int(limit or self._cfg("max_messages_per_diary", 120) or 120)
        need = int(self._cfg("min_messages", 30) or 30)
        groups = self.store.pending_by_person(limit)
        if not groups:
            return {"ok": False, "detail": "没有新的对话"}

        # 挑一个够条件的：攒够条数、而且这人够格（主人或亲密度到位）
        candidates = []
        for uid, rows in groups.items():
            if len(rows) < need:
                continue
            if not self._can_write_for(uid):
                continue
            candidates.append((float(rows[0].get("ts") or 0), uid, rows))
        if not candidates:
            return {"ok": False, "detail": "没有攒够的人"}
        candidates.sort()
        _, uid, rows = candidates[0]

        name = self._person_name(uid, rows)
        is_owner = uid in self._owner_ids()
        folder = self.store.folder_for(uid, name, is_owner)

        fp = self.store.fingerprint(rows)
        if fp and fp == self.store.fingerprint_of(uid):
            return {"ok": False, "detail": "这批内容已经写过了，跳过"}

        provider = self._provider()
        if provider is None:
            return {"ok": False, "detail": "没有可用模型"}

        async with self._lock:
            start = rows[0].get("ts") or time.time()
            end = rows[-1].get("ts") or time.time()
            fmt = "%m-%d %H:%M"
            time_range = (
                f"{datetime.fromtimestamp(start).strftime(fmt)} ~ "
                f"{datetime.fromtimestamp(end).strftime(fmt)}"
            )
            transcript = "\n".join(
                f"{row.get('who', '某人')}：{row.get('text', '')}" for row in rows
            )
            try:
                data = await writer.write_diary(
                    provider,
                    transcript=transcript,
                    time_range=time_range,
                    message_count=len(rows),
                    voice=str(self._cfg("diary_voice", "") or ""),
                    extra=str(self._cfg("persona_extra", "") or ""),
                    companion=("主人" if is_owner else name),
                )
            except Exception as exc:
                logger.warning(f"[diary] 写作失败：{exc}")
                return {"ok": False, "detail": f"写作失败：{exc}"}

            if not (data.get("body") or "").strip():
                return {"ok": False, "detail": "模型没写出正文"}

            meta = {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "range": time_range,
                "messages": len(rows),
                "mood": data.get("mood", ""),
                "title": data.get("title", ""),
                "tags": data.get("tags", []),
                "with": "主人" if is_owner else name,
            }
            path = self.store.save_diary(meta, data["body"], folder)
            # 只记这个人写到哪了，互不影响
            self.store.mark_consumed(uid, last_ts=end, fingerprint=fp)
            moved = self.store.apply_retention(int(self._cfg("retention_days", 0) or 0))
            if moved:
                logger.info(f"[diary] 归档了 {moved} 篇旧日记")
            logger.info(f"[diary] 写完一篇：{folder}/{path.name}")
            return {
                "ok": True,
                "path": str(path),
                "folder": folder,
                "detail": f"写完啦（{folder} · {len(rows)} 条 → {len(data['body'])} 字）",
            }

    # ------------------------------------------------------------- 事件
    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        try:
            text = (getattr(event, "message_str", "") or "").strip()
            if text and not self._is_command_noise(text) and self._should_record(event):
                umo = getattr(event, "unified_msg_origin", None) or event.get_session_id()
                uid = str(event.get_sender_id() or "")
                if self._is_owner(event):
                    who = "主人"
                else:
                    who = event.get_sender_name() or f"QQ尾号{uid[-4:]}"
                self.store.append_chat(umo, uid, who, text)
            self._ensure_loop()
        except Exception as exc:
            logger.debug(f"[diary] 记录消息失败：{exc}")
        if False:
            yield

    @filter.on_decorating_result(priority=9500)
    async def record_self_reply(self, event: AstrMessageEvent):
        """把她自己的回复也记进缓冲——日记里要有她说过的话。"""
        try:
            if not bool(self._cfg("record_self", True)):
                return
            if not self._should_record(event):
                return
            # 指令的回复（/亲密度、/help 的输出）不算聊天
            if self._is_command_noise(getattr(event, "message_str", "") or ""):
                return

            result = event.get_result()
            if result is None:
                return
            parts: list[str] = []
            for comp in getattr(result, "chain", None) or []:
                if isinstance(comp, Plain):
                    piece = (comp.text or "").strip()
                    if piece:
                        parts.append(piece)
            text = "\n".join(parts).strip()
            if not text:
                return
            # 报错提示不是她说过的话
            if any(marker in text for marker in ERROR_MARKERS):
                logger.debug("[diary] 跳过报错提示，不记进素材")
                return

            umo = getattr(event, "unified_msg_origin", None) or event.get_session_id()
            # 她的回复算在「正在和她聊的这个人」名下
            owner_uid = str(event.get_sender_id() or "")
            self.store.append_chat(umo, owner_uid, "大肥鱼", text)
        except Exception as exc:
            logger.debug(f"[diary] 记录自己的回复失败：{exc}")

    @filter.on_llm_request(priority=9000)
    async def inject_diary(self, event: AstrMessageEvent, req):
        try:
            if not bool(self._cfg("inject_enabled", True)):
                return
            uid = str(event.get_sender_id() or "")
            is_owner = self._is_owner(event)

            # 只读「和当前这个人」的日记：主人读主人文件夹，其他人读自己的文件夹
            folder = self.store.folder_of(uid)
            if not folder:
                if is_owner:
                    folder = self.store.owner_folder
                elif self._can_write_for(uid):
                    folder = self.store.folder_for(uid, self._person_name(uid, []), False)
                else:
                    return  # 还没到亲密，没给她写过日记，也就没得读
            if is_owner:
                folder = self.store.owner_folder

            count = int(self._cfg("inject_recent", 2) or 2)
            items = self.store.recent(count, folder)
            if not items:
                return
            limit = int(self._cfg("inject_max_chars", 1500) or 1500)
            header = (
                "【本鲸的日记】"
                if is_owner
                else f"【本鲸关于「{folder}」的日记】"
            )
            chunks = [
                header,
                "下面是本鲸自己写的日记，用来想起之前发生过什么。",
                "- 聊到以前的事，优先从这里找；找不到就直说记不清了。",
                "- 用你自己的话说，别逐字念，也别说“根据我的日记”。",
                "",
            ]
            used = 0
            for item in items:
                head = f"=== {item.get('date', '')}｜{item.get('title', '')} ==="
                body = (item.get("body") or "").strip()
                piece = f"{head}\n{body}"
                if used + len(piece) > limit:
                    piece = piece[: max(0, limit - used)]
                if not piece:
                    break
                chunks.append(piece)
                used += len(piece)
                if used >= limit:
                    break
            block = "\n\n".join(chunks)
            req.system_prompt = (req.system_prompt or "") + "\n\n" + block
        except Exception as exc:
            logger.debug(f"[diary] 注入失败：{exc}")
