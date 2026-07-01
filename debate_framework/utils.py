"""共享工具函数：日志配置、控制台编码修复、辩论加载工厂、进程检测。"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from debater.host import DebateHost
    from .config import TimeConfig


def setup_logging(verbose: bool = False) -> None:
    """配置日志输出到控制台。"""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(console)


def fix_stdout_encoding() -> None:
    """Windows GBK 控制台无法编码部分字符 → 降级替换，避免打印崩溃。"""
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass


def load_debate_from_dir(debate_dir: Path) -> tuple[DebateHost, TimeConfig, dict]:
    """从辩论目录加载配置，创建 DebateHost（跳过备赛）。

    Returns:
        (host, time_config, raw_config)
    """
    from debater.host import DebateHost
    from .config import TimeConfig

    logger = logging.getLogger("debater")
    config_path = debate_dir / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    config = json.loads(config_path.read_text(encoding="utf-8"))
    pro_cfg = config["正方"]
    con_cfg = config["反方"]
    rules = config.get("rules", {})

    time_config = TimeConfig(
        total_free_time=rules.get("free_time_per_team_minutes", 18),
        free_debate_time=rules.get("free_debate_time_per_side_minutes", 4),
        chars_per_minute=rules.get("chars_per_minute", 240),
    )

    host = DebateHost(
        debate_dir=debate_dir,
        pro_config={
            "position": pro_cfg["position"],
            "model": pro_cfg["model"],
            "thinking": pro_cfg["thinking"],
        },
        con_config={
            "position": con_cfg["position"],
            "model": con_cfg["model"],
            "thinking": con_cfg["thinking"],
        },
        time_config=time_config,
        lang_style=rules.get("lang_style", ""),
    )

    host.is_preparation_done = True

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    host.transcript_file = debate_dir / f"transcript_{ts}.jsonl"

    return host, time_config, config


def is_process_alive(pid: int) -> bool:
    """检查指定 PID 的进程是否仍在运行（跨平台）。"""
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(1, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

