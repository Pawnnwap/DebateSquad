"""可插拔 TTS 引擎注册表（插件化）。

一个 TTS 引擎把一段 AI 发言文本变成可播放的语音。内置两种：
  - 'browser'  浏览器内置 speechSynthesis，服务端不合成（零延迟、离线、默认）。
  - 'edge'     edge-tts 在服务端合成 mp3（在线、更高质量、后台异步）。

接入自定义引擎：把一个 *.py 放进  <data_home>/plugins/tts/  ，文件里
    from live import tts
    class MyEngine(tts.TTSEngine): ...
    tts.register(MyEngine())
即可。UI 下拉、配置落库、发言合成全部按引擎 id 取用——自定义引擎自动出现，
无需改动核心代码。
"""

from __future__ import annotations

import abc
import importlib.util
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class TTSEngine(abc.ABC):
    """一个 TTS 引擎的最小契约。子类至少给出 id / label。

    server_side=True 的引擎要实现 synthesize()（服务端产出音频文件）；
    server_side=False 表示由浏览器端朗读（如内置 'browser'），服务端不做事。
    """

    id: str = ""
    label: str = ""
    server_side: bool = True
    ext: str = "mp3"          # 服务端引擎产出的音频扩展名（mp3/wav/ogg…）

    def available(self) -> bool:
        """运行环境是否就绪（依赖/模型齐全）。不就绪的引擎不会被选为缺省。

        必须便宜（只查依赖与文件存在，勿真正加载模型）。客户端引擎恒为 True。
        """
        return True

    def voices(self) -> list[dict]:
        """返回 [{'value': 音色id, 'name': 展示名}]，供每位辩手的音色下拉选择。"""
        return []

    def default_voice(self) -> str:
        vs = self.voices()
        return vs[0]["value"] if vs else ""

    def synthesize(self, text: str, voice: str, out_path: str | Path,
                   rate: str = "+0%") -> bool:
        """把 text 合成到 out_path。成功返回 True。仅 server_side 引擎需要实现。"""
        return False


_ENGINES: dict[str, TTSEngine] = {}
_DEFAULT = "browser"
_loaded = False


def register(engine: TTSEngine, *, default: bool = False) -> None:
    """注册一个引擎（插件在自己的模块里调用）。default=True 时设为缺省引擎。"""
    if not getattr(engine, "id", ""):
        raise ValueError("TTS 引擎必须有非空 id")
    _ENGINES[engine.id] = engine
    if default:
        global _DEFAULT
        _DEFAULT = engine.id


def _ensure_loaded() -> None:
    """懒加载：首次访问注册表时注册内置引擎并扫描插件目录。"""
    global _loaded
    if _loaded:
        return
    _loaded = True
    from . import tts_live  # noqa: F401  导入即注册 browser + edge 内置引擎
    _discover_plugins()


def _discover_plugins() -> None:
    try:
        from . import paths
        plugin_dir = paths.data_home() / "plugins" / "tts"
    except Exception as e:  # noqa: BLE001 插件目录不可用不应拖垮主流程
        logger.debug("跳过 TTS 插件扫描: %s", e)
        return
    if not plugin_dir.is_dir():
        return
    for f in sorted(plugin_dir.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"live_tts_plugin_{f.stem}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)   # 插件在 import 时调用 tts.register(...)
            logger.info("已加载 TTS 插件: %s", f.name)
        except Exception as e:  # noqa: BLE001 单个插件出错不影响其余
            logger.warning("加载 TTS 插件失败 %s: %s", f, e)


def get(engine_id: str) -> TTSEngine | None:
    _ensure_loaded()
    return _ENGINES.get(engine_id)


def engines() -> list[TTSEngine]:
    _ensure_loaded()
    return list(_ENGINES.values())


def default_id() -> str:
    """缺省引擎：首选 _DEFAULT（若就绪），否则按注册顺序取首个就绪引擎。

    这让「最优引擎」可登顶为默认，但其依赖/模型缺失时自动退回（如 browser），
    绝不会因为默认引擎不可用而导致没有语音。
    """
    _ensure_loaded()
    pref = _ENGINES.get(_DEFAULT)
    if pref and pref.available():
        return _DEFAULT
    for eng in _ENGINES.values():       # 注册顺序
        if eng.available():
            return eng.id
    return _DEFAULT if _DEFAULT in _ENGINES else next(iter(_ENGINES), "browser")


def resolve_id(engine_id: str) -> str:
    """把任意输入规整为已注册的引擎 id；未知则回退到缺省引擎。"""
    _ensure_loaded()
    return engine_id if engine_id in _ENGINES else default_id()


def all_voices() -> list[dict]:
    """所有引擎音色去重合并（供音色下拉；自定义引擎的音色自动并入）。"""
    _ensure_loaded()
    seen: set[str] = set()
    out: list[dict] = []
    for e in _ENGINES.values():
        for v in e.voices():
            val = v.get("value")
            if val and val not in seen:
                seen.add(val)
                out.append(v)
    return out


def default_voice() -> str:
    _ensure_loaded()
    e = _ENGINES.get(default_id())
    return e.default_voice() if e else ""


def options() -> list[dict]:
    """供 /api/options 给前端构建引擎下拉（注册顺序即下拉顺序，首项登顶）。"""
    _ensure_loaded()
    did = default_id()
    return [{"id": e.id, "label": e.label or e.id, "ext": e.ext,
             "server_side": bool(e.server_side), "available": bool(e.available()),
             "default": e.id == did}
            for e in _ENGINES.values()]
