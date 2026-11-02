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
        # 刚发生过「日记权限」指令的会话，那一轮不记
        self._admin_umos: dict[str, float] = {}
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

    def _mark_admin_turn(self, event: AstrMessageEvent) -> None:
        """这轮是主人在管日记权限：把刚记进去的那句撤掉，回复也不记。"""
        try:
            umo = getattr(event, "unified_msg_origin", None) or event.get_session_id()
            uid = str(event.get_sender_id() or "")
            # 以这条消息自己的时间戳为准，只撤这一轮，别误伤之前的聊天
            ts = 0.0
            try:
                ts = float(getattr(event.message_obj, "timestamp", 0) or 0)
            except Exception:
                ts = 0.0
            now = time.time()
            if ts <= 0 or ts > now or (now - ts) > 900:
                ts = now - 120
            dropped = self.store.drop_recent(uid, ts - 2) if uid else 0
            self._admin_umos[str(umo)] = now
            if dropped:
                logger.info(f"[diary] 权限指令轮次，撤掉 {dropped} 条缓冲")
        except Exception as exc:
            logger.debug(f"[diary] 标记权限轮次失败：{exc}")

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

    # ------------------------------------------------------- 日记权限
    def _disabled(self) -> dict:
        data = self.store.state.get("disabled")
        if not isinstance(data, dict):
            data = {}
            self.store.state["disabled"] = data
        return data

    def _is_disabled(self, uid: str) -> bool:
        return bool(self._disabled().get(str(uid)))

    def _forced(self) -> dict:
        data = self.store.state.get("forced")
        if not isinstance(data, dict):
            data = {}
            self.store.state["forced"] = data
        return data

    def _status_of(self, uid: str) -> tuple[str, str]:
        """返回（状态, 说明）：writing / locked / off"""
        if self._is_disabled(uid):
            return "off", "已关闭"
        if str(uid) in self._owner_ids():
            return "writing", "在写（主人）"
        if self._forced().get(str(uid)):
            return "writing", "在写（主人手动开的）"
        need = int(self._cfg("intimate_days", 14) or 14)
        days = self._intimate_days(uid)
        if not bool(self._cfg("include_intimate", True)):
            return "locked", "不写（亲密度联动关着）"
        if days >= need:
            return "writing", "在写"
        return "locked", f"还没到亲密（聊过 {days} 天 / 要 {need} 天），暂时不写"

    def _known_people(self) -> dict[str, dict]:
        """汇总所有「有日记或有过记录」的人：{uid: {name, folder, count}}"""
        people: dict[str, dict] = {}
        folders = self.store.state.get("folders") or {}
        for uid, folder in folders.items():
            people[str(uid)] = {
                "name": str(folder),
                "folder": str(folder),
                "count": len(self.store.list_diaries(str(folder))) if folder else 0,
            }
        # 亲密度插件里有名字的也算进来
        try:
            raw = json.loads(self._intimacy_file().read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for uid, rec in raw.items():
                    uid = str(uid)
                    name = str((rec or {}).get("name") or "").strip()
                    if not name or name == uid:
                        name = f"QQ尾号{uid[-4:]}"
                    item = people.setdefault(uid, {"name": name, "folder": "", "count": 0})
                    if not item.get("name") or item["name"].startswith("QQ尾号"):
                        item["name"] = name
        except Exception:
            pass
        # 主人自己一定在表里
        for uid in self._owner_ids():
            item = people.setdefault(uid, {"name": "主人", "folder": self.store.owner_folder, "count": 0})
            item["name"] = "主人"
            item["folder"] = self.store.owner_folder
            item["count"] = len(self.store.list_diaries(self.store.owner_folder))
        return people

    def _match_people(self, keyword: str) -> list[str]:
        """按名字 / 文件夹 / 号码 / 尾号 找人。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        people = self._known_people()
        lowered = keyword.lower()
        hits = []
        for uid, info in people.items():
            fields = {str(info.get("name") or ""), str(info.get("folder") or ""), uid}
            if uid:
                fields.add(f"QQ尾号{uid[-4:]}")
            if any(lowered == f.lower() or (len(lowered) >= 2 and lowered in f.lower()) for f in fields if f):
                hits.append(uid)
        return hits

    def _status_line(self, uid: str, info: dict) -> str:
        _, label = self._status_of(uid)
        folder = info.get("folder") or "（还没建文件夹）"
        return f"{info.get('name', uid)}｜{label}｜{folder}｜{info.get('count', 0)} 篇"

    @filter.llm_tool(name="diary_list_people")
    async def tool_list_people(self, event: AstrMessageEvent) -> str:
        """查看现在给哪些人写日记、哪些人关了、哪些人还没到亲密。主人问「现在在写谁的日记」时用。

        不需要参数，直接返回名单和每个人的状态。
        """
        _ = event
        self._mark_admin_turn(event)
        people = self._known_people()
        if not people:
            return "现在还没有任何人的记录，先聊几句吧。"

        groups: dict[str, list[str]] = {"writing": [], "locked": [], "off": []}
        for uid, info in people.items():
            state, label = self._status_of(uid)
            folder = info.get("folder") or ""
            count = info.get("count", 0)
            detail = f"{info.get('name', uid)}（{label}"
            if state == "writing" and folder:
                detail += f"，{count} 篇"
            detail += "）"
            groups[state].append(detail)

        lines = [f"共认识 {len(people)} 人。"]
        if groups["writing"]:
            lines.append("正在写：" + "、".join(groups["writing"]))
        else:
            lines.append("正在写：暂无")
        if groups["locked"]:
            lines.append("还没解锁：" + "、".join(groups["locked"]))
        if groups["off"]:
            lines.append("主人关掉了：" + "、".join(groups["off"]))
        lines.append("")
        lines.append("只有「正在写」的人才有日记文件夹。")
        return "\n".join(lines)

    @filter.llm_tool(name="diary_set_person")
    async def tool_set_person(self, event: AstrMessageEvent, person: str, enable: bool = True) -> str:
        """打开或关闭某个人的日记权限。主人说「不要给某某写日记了」「恢复给某某写」时用。

        关闭后：和她/他聊天时不写日记、也不读那人的日记，但已经写好的日记文件不会删除。

        Args:
            person(string): 必填。那个人的名字或QQ号，比如「小晴」「QQ尾号0199」。
            enable(boolean): 可选。true=恢复写（默认），false=关闭。
        """
        if not self._is_owner(event):
            return "这个只有主人能改哦。"
        self._mark_admin_turn(event)
        uid_hits = self._match_people(person)
        if not uid_hits:
            names = "、".join(i.get("name", u) for u, i in self._known_people().items()) or "（还没有别人）"
            return f"没找到「{person}」。现在认识的人有：{names}"
        if len(uid_hits) > 1:
            people = self._known_people()
            return "找到好几个人，请说得更具体些：" + "、".join(
                people[u].get("name", u) for u in uid_hits
            )

        uid = uid_hits[0]
        info = self._known_people()[uid]
        name = info.get("name") or uid
        note = ""
        if enable:
            self._disabled().pop(str(uid), None)
            # 还没到亲密的，主人说了就算数，强制打开
            if str(uid) not in self._owner_ids() and self._intimate_days(uid) < int(
                self._cfg("intimate_days", 14) or 14
            ):
                self._forced()[str(uid)] = name
                note = "（还没到亲密，是主人手动开的）"
        else:
            self._disabled()[str(uid)] = name
            self._forced().pop(str(uid), None)
        self.store.save()
        action = "恢复写" if enable else "不再写"
        extra = "" if enable else "，已经写好的日记还在，没删"
        return f"好啦，{name} 的日记{action}了{extra}{note}。"

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
        """主人永远可以；其他人要亲密度够或主人手动开过，且没被关掉。"""
        if self._is_disabled(uid):
            return False
        if uid in self._owner_ids():
            return True
        if self._forced().get(str(uid)):
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
            umo_key = str(getattr(event, "unified_msg_origin", None) or event.get_session_id())
            # 这一轮是「日记权限」指令，她的回复也不记
            marked = self._admin_umos.pop(umo_key, None)
            if marked is not None:
                logger.debug("[diary] 权限指令轮次，回复不记")
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
            # 主人关掉的人：不读
            if self._is_disabled(uid):
                return

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
