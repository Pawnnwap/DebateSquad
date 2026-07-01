#!/usr/bin/env python3
"""实时真人 ⇄ AI 辩论 —— 启动入口。

用法:
    python live_debate.py              # 默认 http://127.0.0.1:8000
    python live_debate.py --port 8080
    python live_debate.py --host 0.0.0.0 --port 8000   # 局域网可访问

浏览器打开后：点击添加辩手（真人/AI）、设好模型与规则、点「备赛」、再点「开始」。
真人轮到发言时可用麦克风（浏览器免费语音识别）或直接打字。
"""
from __future__ import annotations

import argparse
import logging
import os
import platform
import shutil
import sys
from pathlib import Path

from debate_framework.opencode_runner import OPENCODE_BIN
from debate_framework.utils import fix_stdout_encoding, setup_logging
from live.server import serve


def _opencode_available() -> bool:
    """opencode 是否可用：绝对路径存在，或能在 PATH 中找到。"""
    if os.path.isabs(OPENCODE_BIN):
        return Path(OPENCODE_BIN).exists()
    return shutil.which(OPENCODE_BIN) is not None


def _opencode_install_hint() -> str:
    """据本机系统给出 opencode 安装指引（缺失时打印）。"""
    sysname = platform.system()
    if sysname == "Windows":
        steps = (
            "  1) 装 Node.js（https://nodejs.org，LTS 版）\n"
            "  2) 在 PowerShell 运行： npm install -g opencode-ai\n"
            "     或官方安装脚本： irm https://opencode.ai/install.ps1 | iex"
        )
    elif sysname == "Darwin":
        steps = (
            "  1) brew install sst/tap/opencode\n"
            "     或： curl -fsSL https://opencode.ai/install | bash\n"
            "     或先装 Node.js 再： npm install -g opencode-ai"
        )
    else:  # Linux / 其它
        steps = (
            "  1) curl -fsSL https://opencode.ai/install | bash\n"
            "     或先装 Node.js 再： npm install -g opencode-ai"
        )
    return (
        "\n" + "=" * 60 + "\n"
        "  ⚠ 未检测到 opencode CLI —— AI 辩手需要它才能发言。\n"
        f"  本机系统：{sysname or '未知'}。安装方式：\n"
        f"{steps}\n"
        "  装好后登录： opencode auth login\n"
        "  （已装但仍提示？把可执行文件路径设到环境变量 OPENCODE_CLI 即可。）\n"
        + "=" * 60 + "\n"
    )


def main() -> int:
    fix_stdout_encoding()
    frozen = getattr(sys, "frozen", False)
    parser = argparse.ArgumentParser(description="实时真人⇄AI辩论服务")
    parser.add_argument("--host", default="127.0.0.1")
    # 打包运行默认端口 0（让系统选空闲端口，避免端口冲突），并自动打开浏览器。
    parser.add_argument("--port", type=int, default=0 if frozen else 8000)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)
    logging.getLogger("live").setLevel(logging.INFO)

    # opencode 缺失时给出系统对应的安装指引（不阻断启动，UI 仍可看）。
    if not _opencode_available():
        print(_opencode_install_hint())

    # 打包运行时默认自动开浏览器（除非 --no-browser）。
    open_browser = frozen and not args.no_browser
    serve(host=args.host, port=args.port, open_browser=open_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
