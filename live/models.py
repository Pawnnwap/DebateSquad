"""从本机 opencode 实时获取「可用模型 + 每个模型自己的思考强度（variants）」。

`opencode models --verbose` 会逐个输出 `providerID/id` 行后跟一段模型 JSON，其中：
  - capabilities.reasoning：是否支持思考；
  - variants：该模型「自己」支持的思考强度键（如 ['low','medium','high'] / ['none','high'] / []）。

据此为 UI 提供：模型可输入下拉（datalist）+ 思考强度按所选模型动态填充的下拉。
结果带内存缓存（首次约数秒）；可强制刷新。opencode 不可用时回退到内置精简列表。
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from typing import Optional

from debate_framework.opencode_runner import OPENCODE_BIN

logger = logging.getLogger(__name__)

# 思考强度始终包含 "none"（= 不传 --variant，模型默认/不额外思考）。其余来自该模型的 variants。
_BASE_THINKING = "none"

# opencode 不可用时的回退（仍可手动输入任意模型名）。
_FALLBACK: list[dict] = [
    {"value": "opencode/mimo-v2.5-free", "name": "MiMo v2.5 (free)", "thinking": ["none", "low", "medium", "high"]},
    {"value": "opencode/deepseek-v4-flash-free", "name": "DeepSeek v4 Flash (free)", "thinking": ["none", "low", "medium", "high", "max"]},
    {"value": "claude-sonnet-4-5-20250929", "name": "Claude Sonnet 4.5", "thinking": ["none", "low", "medium", "high"]},
]

_cache: Optional[list[dict]] = None
_cache_at: float = 0.0
_lock = threading.Lock()
_TTL = 600.0           # 10 分钟内复用缓存


def _parse_verbose(text: str) -> list[dict]:
    """把 `opencode models --verbose` 的「行 + JSON」混合输出解析为模型列表。"""
    dec = json.JSONDecoder()
    i = 0
    models: list[dict] = []
    while i < len(text):
        b = text.find("{", i)
        if b < 0:
            break
        try:
            obj, end = dec.raw_decode(text[b:])
            i = b + end
        except json.JSONDecodeError:
            i = b + 1
            continue
        if not isinstance(obj, dict) or "id" not in obj or "providerID" not in obj:
            continue
        value = f"{obj['providerID']}/{obj['id']}"
        variants = list((obj.get("variants") or {}).keys())
        thinking = [_BASE_THINKING] + [v for v in variants if v != _BASE_THINKING]
        name = obj.get("name") or obj["id"]
        models.append({
            "value": value,
            "name": f"{name} · {obj['providerID']}",
            "thinking": thinking,
            "reasoning": bool(obj.get("capabilities", {}).get("reasoning")),
        })
    return models


def fetch_models(refresh: bool = False, timeout: int = 30) -> list[dict]:
    """返回 [{value, name, thinking:[...], reasoning}]，按 value 排序。带缓存。"""
    global _cache, _cache_at
    with _lock:
        if (not refresh and _cache is not None
                and time.time() - _cache_at < _TTL):
            return _cache
        try:
            out = subprocess.run(
                [OPENCODE_BIN, "models", "--verbose"],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
            models = _parse_verbose(out.stdout or "")
            if not models:
                raise RuntimeError("opencode 未返回模型")
            models.sort(key=lambda m: m["value"])
            _cache, _cache_at = models, time.time()
            logger.info("已从 opencode 获取 %d 个模型", len(models))
            return models
        except Exception as e:
            logger.warning("获取 opencode 模型失败，使用回退列表: %s", e)
            if _cache is not None:
                return _cache
            return list(_FALLBACK)


def thinking_for(model_value: str) -> list[str]:
    """某模型支持的思考强度列表；未知模型回退到通用四档。"""
    for m in fetch_models():
        if m["value"] == model_value:
            return m["thinking"]
    return [_BASE_THINKING, "low", "medium", "high"]
