#!/usr/bin/env bash
# 构建 macOS 可执行（one-folder）。在项目根目录运行：
#   bash packaging/build_mac.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

safe_rm() {
  local target="$1"
  [[ -e "$target" ]] || return 0
  local resolved
  resolved="$(cd "$(dirname "$target")" && pwd)/$(basename "$target")"
  case "$resolved" in
    "$ROOT"/*) rm -rf "$resolved" ;;
    *) echo "拒绝删除项目目录外的路径: $resolved" >&2; exit 1 ;;
  esac
}

echo "[1/3] 安装/校验依赖..."
python3 -m pip install pyinstaller edge-tts
# 可选：打包 Piper 本机神经语音（显著增大包体）。设 BUNDLE_PIPER=1 启用。
case "${BUNDLE_PIPER:-}" in
  1|true|yes|on|TRUE|YES|ON)
    echo "[1/3] BUNDLE_PIPER 已设 -> 安装 piper-tts 以便打入包内..."
    python3 -m pip install piper-tts
    ;;
esac

echo "[2/3] 清理旧产物..."
if [[ ! -d "$ROOT/debater_prompts/functional_prompts" ]]; then
  echo "缺少功能性 prompt 目录: $ROOT/debater_prompts/functional_prompts" >&2
  exit 1
fi
prompt_count="$(find "$ROOT/debater_prompts/functional_prompts" -maxdepth 1 -type f -name '*.md' | wc -l | tr -d ' ')"
if [[ "$prompt_count" -lt 7 ]]; then
  echo "功能性 prompt 文件不足，预期至少 7 个 markdown 文件。" >&2
  exit 1
fi
for name in "辩论方法论.md" "逻辑与论辩学理论.md" "如何深化辩论.md"; do
  if [[ ! -f "$ROOT/methodology/$name" ]]; then
    echo "缺少必需的方法论文件: $ROOT/methodology/$name" >&2
    exit 1
  fi
done
safe_rm "$ROOT/build/AI-Debate-Live"
safe_rm "$ROOT/dist/AI-Debate-Live"

echo "[3/3] PyInstaller 打包..."
python3 -m PyInstaller packaging/ai_debate_live.spec --noconfirm --distpath dist --workpath build

echo ""
echo "完成 → dist/AI-Debate-Live/AI-Debate-Live"
echo "运行：./dist/AI-Debate-Live/AI-Debate-Live （会自动打开浏览器）"
echo "前提：本机已安装并登录 opencode。"
