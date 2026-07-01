"""跨平台、可写、且打包后仍正确的路径解析。

打包成 exe 后：
  - 静态资源（index.html）随包只读 → 从 PyInstaller 解包目录读取；
  - 运行产物（每场音频）与「辩论库」（保存的辩题/备赛/记录）必须写到用户可写目录，
    绝不能写进 Program Files / .app 内部 → 统一放到用户主目录下的数据目录。

数据目录可用环境变量 AI_DEBATE_HOME 覆盖，便于自定义或测试。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def data_home() -> Path:
    """用户级可写数据根目录（保存库、运行产物都在其下）。"""
    env = os.environ.get("AI_DEBATE_HOME")
    if env:
        return Path(env).expanduser()
    # 主目录始终可写，跨 Windows/macOS/Linux 一致、最稳。
    return Path.home() / ".ai-debate-live"


def library_dir() -> Path:
    """辩论库：每条目录含 meta.json + briefs/ + transcript.jsonl。"""
    p = data_home() / "library"
    p.mkdir(parents=True, exist_ok=True)
    return p


def runs_dir() -> Path:
    """每场实时辩论的工作目录（音频等临时产物）。"""
    p = data_home() / "runs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def static_dir() -> Path:
    """前端静态资源目录（打包后从解包目录读取，否则用源码目录）。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        cand = base / "live" / "static"
        if cand.exists():
            return cand
        return base / "static"
    return Path(__file__).resolve().parent / "static"
