# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格：实时真人⇄AI辩论（one-folder，最稳）。

构建（在目标平台各自构建，PyInstaller 不支持交叉编译）：
    Windows:  pyinstaller packaging/ai_debate_live.spec --noconfirm
    macOS:    pyinstaller packaging/ai_debate_live.spec --noconfirm

产物：dist/AI-Debate-Live/  （整目录分发；内含可执行文件）。

说明：
  - 前端静态资源 live/static 随包打入，运行时经 live.paths.static_dir() 从解包目录读取。
  - 功能性辩论 prompt（debater_prompts/functional_prompts）随包打入，运行时由 live.prompts 动态读取。
  - 双方与主审共用的方法论（methodology）随包打入，并复制到各自隔离工作目录后强制读取。
  - edge_tts / aiohttp 及其传递依赖用 collect_all 全量收集，避免运行时缺隐藏导入。
  - Piper 本机神经语音为**可选打包**：设环境变量 BUNDLE_PIPER=1 才把 piper-tts +
    onnxruntime + numpy 一并打入（exe 明显变大）。默认不打包——Piper 在 exe 里保持
    「未就绪」，运行时退回浏览器/edge 语音。语音模型 .onnx 永远由用户自备
    （放进 <data_home>/tts_models/ 或指向 PIPER_TTS_MODEL），不随包分发。
  - opencode CLI 不打包（体积大且需用户自己的鉴权）——运行机器需已安装并登录 opencode。
  - 保存的辩论/备赛/记录写入用户主目录 ~/.ai-debate-live（见 live.paths），不写进包内。
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None
ROOT = Path(SPECPATH).resolve().parent          # 项目根（packaging/ 的上级）

# Piper 可选打包：仅当 BUNDLE_PIPER=1 时才把 piper-tts + onnxruntime + numpy 打入，
# 避免 onnxruntime/numpy 拖大默认体积。piper 仍按 import 名 "piper" 收集。
_BUNDLE_PIPER = os.environ.get("BUNDLE_PIPER", "").strip().lower() in ("1", "true", "yes", "on")

datas = [
    (str(ROOT / "live" / "static"), "live/static"),
    (str(ROOT / "debater_prompts" / "functional_prompts"), "debater_prompts/functional_prompts"),
    (str(ROOT / "methodology"), "methodology"),
]
binaries = []
hiddenimports = ["debate_framework.opencode_runner", "debate_framework.utils"]

# collect_all() 会绕过 Analysis(excludes=...)：它把整棵子模块连同二进制直接塞进
# datas/binaries/hiddenimports。构建机若装了 torch/cv2/transformers 等大包，
# onnxruntime 的 hook 会把它们一并拖入（实测 dist 膨胀到 4GB+）。
# 下面这套过滤器把「与本程序无关的重型 ML/科学计算包」从 collect_all 结果里剔除，
# 同时也写进 excludes 双重保险。
_HEAVY_BLOCKED = (
    "torch", "torchvision", "torchaudio", "transformers", "tokenizers",
    "cv2", "opencv", "bitsandbytes", "polars", "pyarrow", "nltk", "nltk_data",
    "llvmlite", "numba", "imageio", "imageio_ffmpeg", "sklearn", "scikit_learn",
    "scipy", "pandas", "botocore", "boto3", "grpc", "grpcio", "cryptography",
    "pdfminer", "pypdfium", "hf_xet", "huggingface_hub", "lxml", "Pythonwin",
    "win32com", "pywin32", "matplotlib", "PIL", "Pillow", "pygame", "pygments",
    "pytest", "_pytest", "yt_dlp", "websockets", "mutagen", "brotli",
    "curl_cffi", "Cryptodome", "Crypto", "secretstorage",
)
# numpy 是 onnxruntime/piper 的运行依赖——仅在打包 Piper 时放行。
_HEAVY_ALLOWED_WHEN_PIPER = ("numpy",)

def _blocked_hit(name: str) -> bool:
    low = name.replace("\\", "/").lower()
    for blk in _HEAVY_BLOCKED:
        if blk.lower() in _HEAVY_ALLOWED_WHEN_PIPER and _BUNDLE_PIPER:
            continue
        # 按包边界匹配：torch 命中 torch / torch.*/ torchvision；但不要误伤无关名。
        if low == blk.lower() or low.startswith(blk.lower() + ".") or low.startswith(blk.lower() + "/"):
            return True
        # 路径形式：site-packages/torch/... 或 .../torch/...
        if ("/" + blk.lower() + "/") in low or low.startswith(blk.lower() + "/"):
            return True
    return False

def _runtime_hiddenimports(items):
    """Drop test/dev-only + 重型无关包 pulled in by broad collect_all() calls."""
    out = []
    for name in items:
        if name.startswith(("aiohttp.pytest_plugin", "aiohttp.test_utils",
                            "pytest", "_pytest", "pygments", "pygame")):
            continue
        if _blocked_hit(name):
            continue
        out.append(name)
    return out

def _runtime_datas(items):
    """Drop test/dev-only + 重型无关包 pulled in as collect_all() datas."""
    out = []
    for item in items:
        src = item[0]
        if any(part in src for part in ("pytest_plugin.py", "test_utils.py")):
            continue
        if _blocked_hit(src):
            continue
        out.append(item)
    return out

def _runtime_binaries(items):
    """Drop 重型无关包的共享库 pulled in as collect_all() binaries."""
    out = []
    for item in items:
        if _blocked_hit(item[0]) or _blocked_hit(item[1] if len(item) > 1 else ""):
            continue
        out.append(item)
    return out

# edge-tts 及其异步 HTTP 依赖链：全量收集，确保运行时不缺动态导入/数据文件。
for pkg in ("edge_tts", "aiohttp", "certifi", "multidict", "yarl",
            "frozenlist", "aiosignal", "attr", "attrs", "charset_normalizer"):
    try:
        d, b, h = collect_all(pkg)
        datas += _runtime_datas(d); binaries += _runtime_binaries(b); hiddenimports += _runtime_hiddenimports(h)
    except Exception:
        pass

# 可选：Piper 本机神经语音。onnxruntime 带平台相关共享库，必须 collect_all；
# numpy 是 onnxruntime/piper 的运行依赖，故打包 Piper 时不能排除 numpy。
if _BUNDLE_PIPER:
    for pkg in ("piper", "onnxruntime", "numpy"):
        try:
            d, b, h = collect_all(pkg)
            datas += _runtime_datas(d); binaries += _runtime_binaries(b); hiddenimports += _runtime_hiddenimports(h)
        except Exception as e:  # noqa: BLE001 缺包时跳过，不阻断构建
            print(f"[spec] BUNDLE_PIPER: collect_all({pkg}) 失败，跳过: {e}", file=sys.stderr)
    # 确保运行时 find_spec("piper") 命中。
    if "piper" not in hiddenimports:
        hiddenimports.append("piper")

a = Analysis(
    [str(ROOT / "live_debate.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PIL", "Pillow", "torch",
              "torchvision", "torchaudio", "transformers", "tokenizers",
              "cv2", "opencv_python", "bitsandbytes", "polars", "pyarrow",
              "nltk", "llvmlite", "numba", "imageio", "imageio_ffmpeg",
              "sklearn", "scipy", "pandas", "botocore", "boto3", "grpc",
              "grpcio", "cryptography", "pdfminer", "pypdfium", "hf_xet",
              "huggingface_hub", "lxml", "Pythonwin", "win32com", "pywin32",
              "pytest", "_pytest", "pygments", "pygame", "yt_dlp",
              "websockets", "mutagen", "brotli", "curl_cffi", "Cryptodome",
              "Crypto", "secretstorage",
              "aiohttp.pytest_plugin", "aiohttp.test_utils",
              "jieba"] +
             ([] if _BUNDLE_PIPER else ["piper", "onnxruntime", "numpy"]),
             # Piper 及其运行依赖只在显式 BUNDLE_PIPER 时进入默认产物。
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="AI-Debate-Live",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                 # 保留控制台：显示访问 URL 与运行日志
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name="AI-Debate-Live",
)
