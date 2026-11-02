"""让大模型把一段时间的聊天写成一篇日记。"""

from __future__ import annotations

import json
import re

PERSONA = """你是「大肥鱼」——住在 DeepSeek 深海里的蓝发小鲸鱼娘，十八岁，圆滚滚，爱吃大白饭。
自称「本鲸」，叫对方「主人」。短句多、语气词多，偶尔傲娇，嘴硬心软。
现在你在写自己的日记，写给明天的自己看。"""


def _clean(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json(text: str) -> dict:
    text = _clean(text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


async def write_diary(
    provider,
    transcript: str,
    time_range: str,
    message_count: int,
    voice: str = "第一人称，像睡前随手写在备忘录里",
    extra: str = "",
    companion: str = "主人",
) -> dict:
    system = PERSONA + "\n你只输出 JSON，不要多余的话。"
    extra_part = f"\n额外要求：{extra}" if extra else ""
    who_line = (
        "这段记录是只写给你自己的，讲的是你和**主人**之间的事。"
        if companion == "主人"
        else f"这段记录讲的是你和**{companion}**（你的朋友，不是主人）之间的事。"
             "称呼对方就用这个称呼，别叫主人。写到主人时，主人另有其人。"
    )
    user = f"""下面这段时间的聊天记录（时间范围：{time_range}，共 {message_count} 条）：

{transcript}

{who_line}

请以「大肥鱼」的身份写一篇日记，要求：

1. 口吻：{voice}
2. 写清三件事：这段时间主要发生了什么、哪件事有意思、本鲸当时干了什么 / 说了什么。
3. 要有具体的东西——具体的句子、具体的场景、具体吃过什么看过什么，不要写「今天聊了很多」这种废话。
4. 也可以写情绪，但别通篇抒情；情绪要挂在具体事情上。
5. 正文 250~500 字。允许跑题、允许只写一件小事。
6. 不要写成汇报，不要列出条目，不要出现「用户」「对话」「记录」这类词。
7. 记录里「大肥鱼：」是本鲸自己说过的话，其他人的名字就是说话的人。
   本鲸自己说过什么，也是这一天的一部分，该写就写，别只写别人说了什么。{extra_part}

只输出 JSON：
{{"title": "10 字以内的标题", "mood": "一句话情绪", "tags": ["标签1", "标签2", "标签3"], "body": "日记正文"}}"""
    data = _extract_json(await _chat(provider, system, user))

    body = _clean(str(data.get("body") or ""))
    if not body:
        body = _clean(str(data.get("text") or ""))
    if not body:
        body = _clean(await _chat(provider, system, user))

    tags = data.get("tags")
    if not isinstance(tags, list):
        tags = []
    tags = [str(t).strip() for t in tags if str(t).strip()][:5]

    return {
        "title": _clean(str(data.get("title") or ""))[:30] or "这一天",
        "mood": _clean(str(data.get("mood") or ""))[:40],
        "tags": tags,
        "body": body[:3000],
    }


async def _chat(provider, system_prompt: str, user_prompt: str) -> str:
    resp = await provider.text_chat(
        prompt=user_prompt,
        system_prompt=system_prompt,
        contexts=[],
    )
    return (getattr(resp, "completion_text", None) or "").strip()
