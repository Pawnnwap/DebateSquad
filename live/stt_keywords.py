"""STT contextual keyword extraction.

The browser STT surface can use contextual phrases where supported.  This
module keeps extraction best-effort: opencode/big-pickle is the default, with a
small local fallback so the UI remains useful offline or when the model is down.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from debate_framework.opencode_runner import OpenCodeRunner, generate_session_id

from . import paths, stt

logger = logging.getLogger(__name__)

DEFAULT_KEYWORD_MODEL = "opencode/big-pickle"
MAX_KEYWORDS = 80
_MAX_KEYWORD_LEN = 24

_STOPWORDS = {
    "是否", "应该", "可以", "不能", "不是", "因为", "所以", "如果", "那么",
    "我们", "你们", "他们", "这个", "那个", "一个", "一种", "进行", "通过",
    "问题", "辩题", "规则", "正方", "反方", "真人", "辩手",
}


def _clean_keyword(text: str) -> str:
    s = re.sub(r"^[\s\-\*\d.、，,;；:：\"'`]+|[\s\-\*，,;；:：。.!！?？\"'`]+$", "", text or "")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _dedupe(words: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for word in words:
        w = _clean_keyword(word)
        if not w or w in seen:
            continue
        if len(w) < 2 or len(w) > _MAX_KEYWORD_LEN:
            continue
        if w in _STOPWORDS:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= MAX_KEYWORDS:
            break
    return out


def _parse_keywords(text: str) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    for m in re.finditer(r"\[[\s\S]*?\]", raw):
        try:
            arr = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(arr, list):
            return _dedupe([str(x) for x in arr])
    lines = re.split(r"[\n,，、;；]", raw)
    return _dedupe(lines)


def _local_fallback(topic: str, rules: str, stances: list[str]) -> list[str]:
    text = "\n".join([topic or "", rules or "", "\n".join(stances)])
    candidates: list[str] = []
    candidates.extend(re.findall(r"[A-Za-z][A-Za-z0-9+_.-]{1,24}", text))
    candidates.extend(re.findall(r"[\u4e00-\u9fff]{2,8}", text))
    # Add chunks around separators so short formal terms survive better.
    candidates.extend(re.split(r"[\s,，、;；。.!！?？:：\"'“”‘’（）()《》<>/\\|]+", text))
    return _dedupe(candidates)


def extract_keywords(topic: str, rules: str = "", stances: list[str] | None = None,
                     model: str = DEFAULT_KEYWORD_MODEL) -> dict:
    stances = stances or []
    fallback = _local_fallback(topic, rules, stances)
    if not (topic or rules or stances):
        return {"keywords": [], "source": "empty"}

    prompt = (
        "你要为中文实时语音识别生成上下文关键词，用于提高辩论现场 STT 对专名、术语、缩写的识别准确率。\n"
        "请只输出 JSON 字符串数组，不要解释，不要 Markdown。\n"
        f"最多 {MAX_KEYWORDS} 个；每个词 2-24 字；优先包含专有名词、技术词、政策词、关键概念、易误识别短语。\n"
        "不要输出泛词，如“是否、应该、问题、正方、反方、辩手”。\n\n"
        f"辩题：{topic or '（空）'}\n\n"
        f"规则：{rules or '（空）'}\n\n"
        f"全队立场：{'；'.join([x for x in stances if x]) or '（空）'}\n"
    )
    folder: Path = paths.data_home() / "stt_keywords"
    folder.mkdir(parents=True, exist_ok=True)
    runner = OpenCodeRunner(model=model, timeout=60)
    out = runner.run_safe(
        message=prompt,
        session_id=generate_session_id("stt-keywords"),
        title="STT关键词提取",
        model=model,
        cwd=folder,
        pure=True,
    )
    words = _parse_keywords(out)
    if words:
        merged = _dedupe(words + fallback)
        return {"keywords": merged, "source": model}
    logger.warning("STT 关键词提取失败，使用本地兜底: %s", out[:300])
    return {"keywords": fallback, "source": "local-fallback"}


# ---------------------------------------------------------------------------
# 内置提供方（实现 live.stt.STTProvider 契约）
# ---------------------------------------------------------------------------
class OpenCodeKeywordProvider(stt.STTProvider):
    """用 opencode/big-pickle 提取上下文关键词，失败回退本地启发式。"""

    id = "opencode"
    label = "opencode/big-pickle（默认）"

    def extract(self, topic: str, rules: str = "",
                stances: list[str] | None = None) -> dict:
        return extract_keywords(topic, rules, stances)


stt.register(OpenCodeKeywordProvider(), default=True)
