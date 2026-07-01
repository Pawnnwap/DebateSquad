# 构建 AI-Debate-Live

PyInstaller one-folder 构建，目标是稳定分发实时辩论 Web UI。目标机器不需要 Python，但需要已安装、已鉴权的 OpenCode CLI。

PyInstaller 不支持交叉编译：Windows 在 Windows 构建，macOS 在 macOS 构建。

## Windows

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

产物：

```text
dist\AI-Debate-Live\AI-Debate-Live.exe
```

## macOS

```bash
bash packaging/build_mac.sh
```

产物：

```text
dist/AI-Debate-Live/AI-Debate-Live
```

分发时压缩整个 `dist/AI-Debate-Live/`，不能只发送可执行文件。

## 包内资源

构建脚本会校验，PyInstaller spec 会打入：

- `live/static/`：Web UI
- `debater_prompts/functional_prompts/`：七类功能提示词
- `methodology/`：双方使用的两份通用方法论，以及主审强制使用的《如何深化辩论》
- `edge-tts` 与异步 HTTP 依赖

程序建立辩论时，会把三份方法论复制到每个阵营和主审的隔离工作目录。主审初始化与每次点评 prompt 都明确要求使用《如何深化辩论》。缺少任一文件时构建直接失败。

## 目标机器

```bash
opencode auth login
opencode models
```

程序按以下顺序寻找 OpenCode：

1. `OPENCODE_CLI` 环境变量
2. 常见安装路径
3. `PATH`

默认 TTS 是浏览器 `speechSynthesis`。`edge-tts` 已包含在包内，可在 UI 切换，使用时需要联网。

## 数据目录

辩题、备赛材料、记录和音频写入：

```text
~/.ai-debate-live/
```

可用 `AI_DEBATE_HOME` 覆盖。运行数据不会写进安装目录。

## 可选 Piper

默认不包含 Piper、onnxruntime 和 numpy，避免产物显著增大。

Windows：

```powershell
$env:BUNDLE_PIPER = "1"
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

macOS：

```bash
BUNDLE_PIPER=1 bash packaging/build_mac.sh
```

语音模型 `.onnx` 和同名 `.onnx.json` 不随包分发。放进 `<data_home>/tts_models/`，或用 `PIPER_TTS_MODEL` 指向模型。

## 外部插件

编译版仍会扫描：

```text
<data_home>/plugins/tts/*.py
<data_home>/plugins/stt/*.py
```

插件不随默认产物分发。加载失败只记录警告，不阻断核心应用。
