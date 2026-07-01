"""可插拔 STT 关键词提供方（插件化）。

现场语音识别在浏览器端（Web Speech）完成；本框架负责生成「上下文关键词」用于
提高对专名/术语/缩写的识别准确率（contextual biasing）。内置一种：
  - 'opencode'  用 opencode/big-pickle 提取，带本地兜底。

接入自定义提供方：把一个 *.py 放进  <data_home>/plugins/stt/  ，文件里
    from live import stt
    class MyProvider(stt.STTProvider): ...
    stt.register(MyProvider())
即可。/api/options 会列出全部提供方，前端下拉自动出现。
"""

from __future__ import annotations

import abc
import importlib.util
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class STTProvider(abc.ABC):
    """关键词提供方契约：据辩题/规则/立场产出一组识别辅助关键词。"""

    id: str = ""
    label: str = ""

    @abc.abstractmethod
    def extract(self, topic: str, rules: str = "",
                stances: list[str] | None = None) -> dict:
        """返回 {'keywords': [...], 'source': '...'}。"""
        raise NotImplementedError


_PROVIDERS: dict[str, STTProvider] = {}
_DEFAULT = "opencode"
_loaded = False


def register(provider: STTProvider, *, default: bool = False) -> None:
    if not getattr(provider, "id", ""):
        raise ValueError("STT 提供方必须有非空 id")
    _PROVIDERS[provider.id] = provider
    if default:
        global _DEFAULT
        _DEFAULT = provider.id


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from . import stt_keywords  # noqa: F401  导入即注册内置 opencode 提供方
    _discover_plugins()


def _discover_plugins() -> None:
    try:
        from . import paths
        plugin_dir = paths.data_home() / "plugins" / "stt"
    except Exception as e:  # noqa: BLE001
        logger.debug("跳过 STT 插件扫描: %s", e)
        return
    if not plugin_dir.is_dir():
        return
    for f in sorted(plugin_dir.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"live_stt_plugin_{f.stem}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            logger.info("已加载 STT 插件: %s", f.name)
        except Exception as e:  # noqa: BLE001
            logger.warning("加载 STT 插件失败 %s: %s", f, e)


def get(provider_id: str) -> STTProvider | None:
    _ensure_loaded()
    return _PROVIDERS.get(provider_id)


def default_id() -> str:
    _ensure_loaded()
    return _DEFAULT if _DEFAULT in _PROVIDERS else next(iter(_PROVIDERS), "opencode")


def resolve_id(provider_id: str) -> str:
    _ensure_loaded()
    return provider_id if provider_id in _PROVIDERS else default_id()


def extract(provider_id: str, topic: str, rules: str = "",
            stances: list[str] | None = None) -> dict:
    """用指定提供方提取（未知 id 回退缺省）。"""
    _ensure_loaded()
    prov = _PROVIDERS.get(provider_id) or _PROVIDERS.get(default_id())
    if not prov:
        return {"keywords": [], "source": "none"}
    return prov.extract(topic, rules, stances)


def options() -> list[dict]:
    _ensure_loaded()
    return [{"id": p.id, "label": p.label or p.id, "default": p.id == default_id()}
            for p in _PROVIDERS.values()]
