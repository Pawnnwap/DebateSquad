"""实时真人 ⇄ AI 辩论模块（live human-vs-AI debate）。

本模块提供：
  - 任意人数 / 任意阵营 / 任意赛制（自定义规则）的实时辩论编排（engine）；
  - 真人辩手通过浏览器麦克风（Web Speech API，免费）或打字参与；
  - AI 发言用 edge-tts（免费）实时合成播报；主持人 / 主审发言不合成语音；
  - 可选「主审」与「简洁复核」开关；
  - 双方与主审共享、打包内置的方法论；
  - stdlib HTTP + SSE 服务（无新依赖）+ 单页 Web UI。

AI 调用由 debate_framework.OpenCodeRunner 统一交给本机 OpenCode CLI。
"""

from .engine import DebaterCfg, LiveConfig, LiveDebate, PhaseSpec

__all__ = ["DebaterCfg", "LiveConfig", "LiveDebate", "PhaseSpec"]
