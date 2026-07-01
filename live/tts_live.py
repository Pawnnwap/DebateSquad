"""内置 TTS 引擎：'browser'（浏览器朗读）与 'edge'（edge-tts 服务端合成）。

模块导入即把两者注册进 `live.tts` 注册表。底层 edge 合成函数 `synthesize` /
音色表 `FREE_VOICES` / `DEFAULT_VOICE` 保持公开，供旧调用与自定义引擎复用。
主持人 / 主审发言不在此合成（需求：主持人不配语音）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import threading
import wave
from pathlib import Path

import edge_tts

from . import tts

logger = logging.getLogger(__name__)

# 免费 edge-tts 中文 neural 音色（供 UI 下拉选择）。name 为展示名，value 为 edge voice id。
FREE_VOICES: list[dict[str, str]] = [
    {"value": "zh-CN-XiaoxiaoNeural", "name": "晓晓（女·标准）"},
    {"value": "zh-CN-XiaoyiNeural", "name": "晓伊（女·亲和）"},
    {"value": "zh-CN-YunxiNeural", "name": "云希（男·沉稳）"},
    {"value": "zh-CN-YunyangNeural", "name": "云扬（男·播音）"},
    {"value": "zh-CN-YunjianNeural", "name": "云健（男·浑厚）"},
    {"value": "zh-CN-liaoning-XiaobeiNeural", "name": "晓北（女·东北腔）"},
    {"value": "zh-CN-shaanxi-XiaoniNeural", "name": "晓妮（女·陕西腔）"},
    {"value": "zh-TW-HsiaoChenNeural", "name": "曉臻（女·台湾腔）"},
]

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
_TTS_RETRIES = 3
_synth_lock = threading.Lock()


def voice_ids() -> set[str]:
    return {v["value"] for v in FREE_VOICES}


async def _synth_async(text: str, voice: str, rate: str, out_path: str) -> bool:
    comm = edge_tts.Communicate(text, voice, rate=rate)
    got = False
    with open(out_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk.get("type") == "audio":
                f.write(chunk["data"])
                got = True
    return got


def synthesize(text: str, voice: str, out_path: str | Path,
               rate: str = "+0%") -> bool:
    """合成一段语音到 out_path(mp3)。成功 True，失败 False（前端将退回纯文本）。

    edge-tts 偶发限流/网络失败 → 重试几次。空文本直接判失败（无需播放）。
    """
    clean = (text or "").strip()
    out_path = str(out_path)
    if not clean:
        return False
    voice = voice if voice in voice_ids() else DEFAULT_VOICE
    for attempt in range(1, _TTS_RETRIES + 1):
        try:
            # edge-tts 内部用 asyncio；不同线程各自 asyncio.run 互不干扰，
            # 但加锁串行化避免偶发的并发限流叠加（实时场景一次只播一段，串行无损体验）。
            with _synth_lock:
                ok = asyncio.run(_synth_async(clean, voice, rate, out_path))
            if ok and Path(out_path).stat().st_size > 0:
                return True
        except Exception as e:
            logger.warning("[tts] 合成失败 (%d/%d): %s", attempt, _TTS_RETRIES, e)
    return False


# ---------------------------------------------------------------------------
# 内置引擎（实现 live.tts.TTSEngine 契约）
# ---------------------------------------------------------------------------
class BrowserTTSEngine(tts.TTSEngine):
    """浏览器 speechSynthesis 朗读：服务端不合成，零延迟、离线、最快（默认）。"""

    id = "browser"
    label = "浏览器语音（最快·离线，默认）"
    server_side = False

    def voices(self) -> list[dict]:
        # 浏览器按 lang 自动挑系统音色；沿用同一份展示列表作为提示/标签。
        return FREE_VOICES

    def default_voice(self) -> str:
        return DEFAULT_VOICE


class EdgeTTSEngine(tts.TTSEngine):
    """edge-tts 在服务端异步合成 mp3：在线、更高质量。"""

    id = "edge"
    label = "edge-tts（在线·更高质量，后台合成）"
    server_side = True

    def voices(self) -> list[dict]:
        return FREE_VOICES

    def default_voice(self) -> str:
        return DEFAULT_VOICE

    def synthesize(self, text: str, voice: str, out_path: str | Path,
                   rate: str = "+0%") -> bool:
        return synthesize(text, voice, out_path, rate)


class PiperTTSEngine(tts.TTSEngine):
    """Piper 本机神经语音：完全离线、免费(MIT)、CPU 上数倍实时——

    比浏览器音色自然，又无 edge-tts 的联网往返延迟（端到端更快、可流式）。
    需用户自备：`pip install piper-tts` + 一个中文语音模型 .onnx（含同名 .onnx.json）。
    模型查找顺序：环境变量 PIPER_TTS_MODEL > <data_home>/tts_models/ 下首个 *.onnx。
    依赖/模型缺失时 available()=False，自动退回浏览器引擎，不影响使用。
    """

    id = "piper"
    label = "Piper 本机神经语音（最快·离线·免费）"
    server_side = True
    ext = "wav"

    def _model(self) -> Path | None:
        env = os.environ.get("PIPER_TTS_MODEL")
        if env and Path(env).exists():
            return Path(env)
        try:
            from . import paths
            folder = paths.data_home() / "tts_models"
            if folder.is_dir():
                found = sorted(folder.glob("*.onnx"))
                if found:
                    return found[0]
        except Exception:  # noqa: BLE001 路径不可用不应影响判断
            pass
        return None

    def available(self) -> bool:
        return importlib.util.find_spec("piper") is not None and self._model() is not None

    def voices(self) -> list[dict]:
        return [{"value": "piper-default", "name": "Piper 中文音色（本机模型）"}]

    def default_voice(self) -> str:
        return "piper-default"

    def synthesize(self, text: str, voice: str, out_path: str | Path,
                   rate: str = "+0%") -> bool:
        text = (text or "").strip()
        model = self._model()
        if not text or not model:
            return False
        try:
            from piper.voice import PiperVoice
            v = PiperVoice.load(str(model))     # 同名 .onnx.json 自动加载
            with wave.open(str(out_path), "wb") as wf:
                # 兼容 piper-tts 不同版本的合成入口。
                if hasattr(v, "synthesize_wav"):
                    v.synthesize_wav(text, wf)
                else:
                    v.synthesize(text, wf)
            return Path(out_path).exists() and Path(out_path).stat().st_size > 0
        except Exception as e:  # noqa: BLE001 合成失败回退纯文本
            logger.warning("[piper] 合成失败: %s", e)
            return False


# 注册顺序即下拉顺序：Piper 登顶并设为「首选默认」。其依赖/模型缺失时，
# default_id() 会自动退回到下一个就绪引擎（browser），不会因此没有语音。
tts.register(PiperTTSEngine(), default=True)
tts.register(BrowserTTSEngine())
tts.register(EdgeTTSEngine())
