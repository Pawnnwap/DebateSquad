# DebateSquad

赛前想模辩，却总凑不齐对手方？队友轮流分饰两角，练着练着就容易变成背稿。

DebateSquad 是面向辩论队与辩论爱好者的实时 AI 模辩桌面应用。它让大模型分别承担正方、反方和中立主审，按你配置的赛制完成一场有准备、有攻防、有复盘的模辩。它不只是两个聊天机器人轮流说话：双方先独立备赛，再在持续会话中听取、回应、追问与总结；主审按完整环节统一点评，把讨论推向机制、前提、代价、边界和反例。

仓库围绕编译后的 `AI-Debate-Live` 产品维护：保留运行源码、Web UI、功能提示词、共享方法论和构建脚本；不收录演示视频工程、私有素材或旧批处理工具。

## 编译版能做什么

- **完整模辩流程**：申论、延续申论、质询、接质、质询小结、自由辩论、总结陈词均可组合。
- **赛制自由编排**：增删模块，设置顺序、时长、阵营、辩位和提示；支持双方镜像编排。
- **任意阵容**：一对一、整队、多阵营均可；每个席位可设为 AI 或真人。
- **真人混合上场**：真人可用浏览器麦克风识别或直接打字，和 AI 在同一赛程里交锋。
- **按阵营独立备赛**：自动完成概念界定、资料搜集、论点构建、证据核验和攻防预演，生成可复用简报。
- **共享方法论**：正反双方必须读取通用方法论；中立主审还必须使用《如何深化辩论》；编译版同样包含全部文件。
- **功能型提示词**：辩手按本场实际承担的功能加载提示，不再被固定为一辩、二辩、三辩或四辩。
- **中立主审**：完整环节结束后统一点评，不在质询的一问一答之间频繁打断。
- **上下文连续**：每名 AI 辩手拥有独立持续会话，能看到自己尚未回应的新发言。
- **模型与思考强度可配**：自动读取本机 OpenCode 可用模型；每名辩手和主审可使用不同模型与思考档位。
- **人设与自定义 prompt**：每个 AI 可单独设定口吻、人设，或直接提供 system prompt。
- **语音能力**：默认用浏览器语音即时朗读；可切换在线 `edge-tts`，也支持本地插件引擎。
- **语音识别辅助**：自动提取辩题术语，帮助浏览器识别专名、缩写和关键概念。
- **现场控制**：暂停、继续、停止、逐句手动推进、语音开关和实时发言查看。
- **简洁复核**：可让 AI 在正式输出前再压缩一次，减少重复、口水话与空泛排比。
- **自动保存与复用**：保存双方简报、赛制、逐轮交锋和主审点评；下次可载入同一场并跳过备赛。
- **记录导出**：赛后直接导出 Markdown，便于复盘、批注和团队讨论。

## 使用编译版

编译产物是 one-folder 应用，不要求目标机器安装 Python。OpenCode CLI 是独立的 AI 后端，仍需先安装并完成模型/provider 鉴权。

### 1. 准备 OpenCode

```bash
opencode auth login
opencode models
```

`opencode models` 能列出模型，才说明 DebateSquad 可以调用它们。OpenCode 可能提供免费模型，也可使用你已配置的其他 provider；具体名单和额度以本机命令输出为准。

若程序未自动找到 OpenCode，可设置：

```powershell
$env:OPENCODE_CLI = "C:\path\to\opencode.exe"
```

### 2. 启动

Windows：

```powershell
.\dist\AI-Debate-Live\AI-Debate-Live.exe
```

macOS：

```bash
./dist/AI-Debate-Live/AI-Debate-Live
```

编译版默认选择空闲端口并自动打开浏览器。控制台会显示访问地址和保存目录。

可选参数：

```text
--host 127.0.0.1     监听地址
--port 8080          固定端口；0 表示自动选择
--no-browser         不自动打开浏览器
--verbose            输出调试日志
```

> 使用 `--host 0.0.0.0` 可让局域网设备访问，但服务本身没有账号认证。只应在可信网络使用。

### 3. 完成一场模辩

1. 写下辩题，为各阵营填写要捍卫的具体持方。
2. 添加辩手，选择 AI/真人、模型、思考强度、音色和人设。
3. 组合赛制模块；需要正式新国辩式流程时，依次加入申论、质询、小结、自由辩论和结辩。
4. 点击“让 AI 备赛”。双方分别研究，互不共享立场材料，但共同使用同一套辩论方法论。
5. 开始比赛。AI 自动回应新内容；真人轮次可说话或打字；主审在完整环节后点评。
6. 赛后查看双方简报、逐轮交锋和主审意见，导出记录；下次载入即可复用备赛材料。

## 为什么备赛不是装饰

真正影响模辩质量的，往往是上场前有没有材料。DebateSquad 为每个阵营建立隔离工作目录，并依次完成：

1. **辩题分析**：界定概念、确定判准、识别核心争点。
2. **资料搜集**：查找论文、统计、案例和权威观点，记录时效与出处。
3. **论点构建**：形成主张—理由—证据链，并准备立论框架。
4. **攻防演练**：预判对方主攻方向，准备反驳、追问和防守边界。
5. **交接简报**：压缩成能直接注入本阵营所有 AI 辩手的场上材料。

同阵营 AI 共享本方研究文件；不同阵营目录隔离。场上辩手可读取与当前环节最相关的材料，而不是仅靠模型临场编造。

## 共享方法论如何生效

仓库中的三份方法论会被 PyInstaller 打进编译产物：

- `methodology/辩论方法论.md`
- `methodology/逻辑与论辩学理论.md`
- `methodology/如何深化辩论.md`

建立一场辩论时，程序把三份文件复制到每个阵营和主审的隔离工作目录，并在初始化 prompt 中强制指定用途：

- **双方备赛**使用破题、资料分级、论点构建、证据核验和攻防预演框架。
- **双方场上发言**使用质询、驳论、自由辩论、总结陈词和逻辑谬误检查。
- **中立主审**必须以《如何深化辩论》为首要执行规范，按“对齐、检验、深化、找支点、综合”推进，并用“进展 / 分歧 / 下一问”组织点评；另外两份方法论用于补强逻辑判断。

构建脚本会在打包前检查三份文件；缺失时直接停止构建，避免出现“源码有方法论、编译版没有”的静默退化。

## 赛制与发言机制

### 功能型辩手

每个赛制模块声明实际功能，程序据此给对应辩手加载完整功能 prompt：

| 功能 | 目标 |
|---|---|
| 开篇申论 | 定义、判准、框架、核心论点 |
| 延续申论 | 驳论并继续深化己方论证 |
| 质询 | 简短提问、控制节奏、逼出承诺或矛盾 |
| 接质 | 正面回答、守住立场、拆除问题预设 |
| 质询小结 | 提炼质询所得并转成己方结论 |
| 自由辩论 | 先回应上一句，再推进己方战场 |
| 总结陈词 | 收束关键交锋、比较双方、价值升华 |

同一辩手可在一场比赛承担多个功能。人数不足时也能完成完整训练，不需要硬凑固定四辩。

### 环节级主审

主审等待一个完整环节结束后再点评：

- 镜像申论：等双方都说完。
- 质询：等该段全部提问与作答结束。
- 自由辩论：等全部交替轮次结束。
- 单独陈词：该发言结束后点评。

这样保留交锋节奏，又能指出双方共同暴露的定义争夺、未回应问题、证据缺口和逻辑跳跃。

### 自由辩论

自由辩论严格按阵营交替。即使双方人数不同，也不会连续安排同一阵营发言；阵营内部再按席位轮换。

## 模型、语音与扩展

### 模型

应用通过 `opencode models --verbose` 读取本机可用模型及各模型支持的思考强度。下拉内容可刷新，也可手动输入模型 ID。

默认列表只用于 OpenCode 暂时不可用时的界面回退，不代表模型永久免费或永久存在。

### TTS

- `browser`：默认；浏览器 `speechSynthesis`，启动快，无服务端音频生成。
- `edge`：在线 `edge-tts`，后台生成音频，不阻塞文字出现。
- 自定义：把注册插件放到 `<data_home>/plugins/tts/`。

可选 Piper 本地神经语音能在构建时加入，但会显著增大产物；模型文件仍由使用者自行提供。

### STT

真人语音识别由浏览器 Web Speech 完成。应用可据辩题、规则和持方生成辅助关键词；浏览器支持 contextual biasing 时，会优先识别这些术语。也可把自定义关键词提供方放到 `<data_home>/plugins/stt/`。

## 保存位置

默认数据目录：

```text
~/.ai-debate-live/
├── library/                 # 已保存辩论、配置、简报、记录
└── runs/                    # 当前运行目录与语音文件
```

Windows 对应 `C:\Users\<用户名>\.ai-debate-live\`。

可用环境变量改位置：

```powershell
$env:AI_DEBATE_HOME = "D:\DebateSquadData"
```

保存内容留在本机。发送给 OpenCode provider 的 prompt、发言和备赛请求受该 provider 自身隐私政策约束。

## 构建

PyInstaller 不支持跨平台交叉编译：Windows 产物需在 Windows 构建，macOS 产物需在 macOS 构建。

### Windows

```powershell
git clone https://github.com/Pawnnwap/DebateSquad.git
cd DebateSquad
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

产物：

```text
dist\AI-Debate-Live\AI-Debate-Live.exe
```

### macOS

```bash
git clone https://github.com/Pawnnwap/DebateSquad.git
cd DebateSquad
bash packaging/build_mac.sh
```

产物：

```text
dist/AI-Debate-Live/AI-Debate-Live
```

分发时必须打包整个 `AI-Debate-Live/` 目录，不能只发送单个可执行文件。

### 可选：内置 Piper

Windows：

```powershell
$env:BUNDLE_PIPER = "1"
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

macOS：

```bash
BUNDLE_PIPER=1 bash packaging/build_mac.sh
```

## 源码运行

用于开发或调试：

```bash
python -m venv .venv
python -m pip install -e .
python live_debate.py
```

源码模式默认打开 `http://127.0.0.1:8000`。

## 仓库结构

```text
DebateSquad/
├── live_debate.py                    # 编译版入口
├── live/
│   ├── server.py                     # HTTP API、SSE、静态资源与导出
│   ├── engine.py                     # 备赛、赛制、发言、主审状态机
│   ├── prompts.py                    # 功能 prompt 与角色 prompt
│   ├── methodology.py                # 方法论校验、复制与使用约束
│   ├── library.py                    # 本地辩论库
│   ├── models.py                     # OpenCode 模型发现
│   ├── tts.py / tts_live.py          # TTS 注册表与内置引擎
│   ├── stt.py / stt_keywords.py      # STT 关键词注册表与内置提供方
│   └── static/index.html             # 单页 Web UI
├── debate_framework/
│   ├── opencode_runner.py            # OpenCode 调用、会话、重试与解析
│   └── utils.py                      # 编码与日志
├── debater_prompts/functional_prompts/
│   └── *.md                          # 七类场上功能提示词
├── methodology/
│   ├── 辩论方法论.md
│   ├── 逻辑与论辩学理论.md
│   └── 如何深化辩论.md
├── packaging/
│   ├── ai_debate_live.spec
│   ├── build_windows.ps1
│   ├── build_mac.sh
│   └── README.md
├── pyproject.toml
└── LICENSE
```

## 技术边界

- AI 能力、速度、费用和联网能力取决于所选 OpenCode 模型/provider。
- 浏览器语音识别能力因浏览器和操作系统而异；打字始终可用。
- AI 生成的事实和数据仍可能出错。方法论会要求交叉核验，但重要材料应由真人复核。
- 应用是训练工具，不能替代真实队友、教练、评委与赛场经验。

## License

MIT
