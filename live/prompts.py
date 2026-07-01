"""真人 ⇄ AI 实时辩论的提示词生成。

两条路径，任选其一（见 DebaterCfg.custom_prompt）：
  1. 自动生成：根据辩题 / 阵营 / 自定义规则 / 人设，拼出该 AI 辩手的 system prompt；
  2. 直接给定：用户在 UI 里粘贴整段 system prompt → 原样使用（覆盖自动生成）。

另含传统「完整备赛」提示：按辩题分析、资料搜集、论点构建、攻防演练、
最终交接简报五步执行，供「点击让 AI 备赛」时使用。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import sys
from typing import TYPE_CHECKING

from . import methodology

if TYPE_CHECKING:
    from .engine import DebaterCfg, LiveConfig

TRADITIONAL_PREP_BRIEF_MAX_CHARS: int = 1500


# ---------------------------------------------------------------------------
# 功能性 prompt（取代旧的「辩位」prompt）
# 由于赛制完全模块化，辩手不再绑定一/二/三/四辩，而是按其在赛制中实际承担的「功能」
# 动态加载对应的职责说明。覆盖竞赛辩论的全部功能环节（含网搜核对的华语辩论标准环节）：
#   开篇申论(立论) / 延续申论(含驳论·深化) / 质询小结(攻辩小结) / 总结陈词(结辩) /
#   质询(攻辩·提问) / 接质(答辩) / 自由辩论。
# ---------------------------------------------------------------------------
FUNCTION_LABELS: dict[str, str] = {
    "opening": "开篇申论",
    "continuation": "延续申论",
    "cross_summary": "质询小结",
    "closing": "总结陈词",
    "question": "质询",
    "answer": "接质",
    "free": "自由辩论",
}

# 每个功能对应一个完整 markdown prompt 文件；短字符串仅作为文件缺失时的兜底。
FUNCTION_PROMPT_FILES: dict[str, str] = {
    "opening": "opening.md",
    "continuation": "continuation.md",
    "cross_summary": "cross_summary.md",
    "closing": "closing.md",
    "question": "question.md",
    "answer": "answer.md",
    "free": "free.md",
}

# 注入 system prompt 的兜底职责说明（让辩手提前知道自己在本场会承担哪些功能，并据此发挥）。
FUNCTION_FRAGMENTS: dict[str, str] = {
    "opening": "【开篇申论】开宗明义：界定关键概念与判准，搭建己方论证框架，亮出 2-3 个核心论点及其最有力论据。立场清晰、结构分明、为全队定调。",
    "continuation": "【延续申论】在己方框架上推进：先精准反驳对方立论的要害（驳论），再补强己方论据、回应对方预设的质疑，把论证推向更深层（机制 / 前提 / 代价 / 边界）。",
    "cross_summary": "【质询小结】凝练复盘刚才的质询交锋：点出对方暴露的漏洞、回避与自相矛盾之处，归纳己方质询所得，并转化为对己方有利的结论。简短、有力、不复述。",
    "closing": "【总结陈词】结辩升华：回应全场关键交锋、指出对方始终未能回应的核心问题，重申己方为何成立，最后落到价值层面收束全场。",
    "question": "【质询】作为质询方：提出简短、犀利、可层层追问的问题，直指对方逻辑漏洞、事实错误或自相矛盾；只问不论、掌控节奏，逼出对方破绽，绝不被对方反客为主。",
    "answer": "【接质（答辩）】作为被质询方：正面、简洁、稳健地作答，不回避、不反问、不偷换概念；守住己方立场、化解对方设下的圈套，必要时点到为止。",
    "free": "【自由辩论】双方交替、快节奏交锋：每次发言短促有力，先回应对方上一句、再推进己方，紧扣争议焦点，不纠缠枝节、不原地打转。",
}

# 各功能对应的「本轮发言」指令（statement 类按 function 区分；其余按 kind）。
_STATEMENT_ASK: dict[str, str] = {
    "opening": "请发表开篇申论：界定关键概念与判准，搭建己方论证框架，亮出核心论点与最有力的论据。",
    "continuation": "请发表申论：先精准反驳对方立论的要害，再深化己方论证、回应对方质疑。",
    "cross_summary": "请做质询小结：复盘刚才的质询交锋，点出对方漏洞与回避之处，归纳己方所得并转为有利结论。",
    "closing": "请做总结陈词：回应全场交锋、指出对方未答的核心问题，重申己方成立，并升华价值收尾。",
}


def _source_prompt_dir() -> Path:
    """功能 prompt 资源目录；兼容源码运行与 PyInstaller 解包目录。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        for cand in (
            base / "debater_prompts" / "functional_prompts",
            base / "functional_prompts",
        ):
            if cand.exists():
                return cand
    return Path(__file__).resolve().parent.parent / "debater_prompts" / "functional_prompts"


@lru_cache(maxsize=None)
def _load_function_prompt(func_key: str) -> str:
    """读取完整功能 prompt；失败时返回短兜底，保证运行不中断。"""
    filename = FUNCTION_PROMPT_FILES.get(func_key, "")
    if filename:
        path = _source_prompt_dir() / filename
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    return FUNCTION_FRAGMENTS.get(func_key, "")


def functions_section(func_keys: list[str], topic: str = "") -> str:
    """把一名辩手在本场承担的功能集合，拼成 system prompt 里的「职责」段落。"""
    seen = []
    for k in func_keys:
        if k in FUNCTION_LABELS and k not in seen:
            seen.append(k)
    if not seen:
        return ""
    blocks = []
    for k in seen:
        prompt = _load_function_prompt(k)
        if topic:
            prompt = prompt.replace("{{DEBATE_TOPIC}}", topic)
        blocks.append(prompt)
    names = "、".join(FUNCTION_LABELS[k] for k in seen)
    return (
        "## 你在本场承担的功能（按赛制自动分配，务必按各功能要求发言）\n"
        f"你将先后承担：{names}。下面是对应的完整功能 prompt，请按本场实际环节执行。\n\n"
        + "\n\n---\n\n".join(blocks)
        + "\n\n"
    )


def build_system_prompt(d: "DebaterCfg", cfg: "LiveConfig",
                        opponents: list[str], teammates: list[str],
                        brief: str = "", functions: list[str] | None = None,
                        prep_manifest: str = "") -> str:
    """生成（或返回用户自定义的）AI 辩手 system prompt。

    Args:
        d: 该辩手配置。
        cfg: 全局辩论配置（辩题、规则、语速等）。
        opponents: 对方阵营名列表（用于告知对手是谁）。
        teammates: 同阵营队友名列表。
        brief: 已有备赛简报（现备或从库加载）；非空则注入，作为发言弹药。
        functions: 该辩手在本场承担的功能键列表（据赛制自动推导）；据此加载功能性职责说明。
        prep_manifest: 该辩手工作目录中可 read 的传统备赛文件清单。
    """
    func_sec = functions_section(functions or [], cfg.topic)
    methodology_sec = methodology.prompt_instructions("debater")
    if d.custom_prompt.strip():
        cp = d.custom_prompt.strip()
        cp += f"\n\n{methodology_sec.rstrip()}"
        if func_sec:
            cp += f"\n\n{func_sec.rstrip()}"
        if brief.strip():
            cp += f"\n\n## 你的备赛简报（务必善用，勿凭空发挥）\n{brief.strip()}"
        if prep_manifest.strip():
            cp += (
                "\n\n## 可查阅的传统备赛资料（路径相对你的工作目录）\n"
                f"{prep_manifest.strip()}\n"
                "发言前优先 read 与本环节最相关的 1-2 个文件，直接使用其中的论点、数据、案例和反驳预案。"
            )
        return cp

    persona = ""
    if d.persona.strip():
        persona = (
            "## 角色人设（贯穿全程，最高优先级）\n"
            f"{d.persona.strip()}\n"
            "你每次发言都必须以该角色的身份、口吻、性格发言；既要像这个角色，"
            "又要把论点讲清楚、论证严密。不要跳出角色，不要解释自己在扮演角色。\n\n"
        )

    rules = ""
    if cfg.rules.strip():
        rules = f"## 本场自定义规则（务必遵守）\n{cfg.rules.strip()}\n\n"

    brief_sec = ""
    if brief.strip():
        brief_sec = (
            "## 你的备赛简报（务必善用，发言时直接引用其论点/数据/案例，勿凭空发挥）\n"
            f"{brief.strip()}\n\n"
        )
    manifest_sec = ""
    if prep_manifest.strip():
        manifest_sec = (
            "## 可查阅的传统备赛资料（路径相对你的工作目录）\n"
            f"{prep_manifest.strip()}\n\n"
            "- 简报给方向，文件给弹药。每次发言前，优先 read 与本环节最相关的 1-2 个文件。\n"
            "- 只读取资料，不要在比赛阶段编辑、创建、删除或移动文件。\n\n"
        )

    opp = "、".join(opponents) if opponents else "对方"
    team = "、".join(t for t in teammates if t != d.name)
    team_line = f"你的队友：{team}\n" if team else ""

    return f"""你正在参加一场实时辩论，对手中包含真人，也可能包含其它 AI。

辩题：{cfg.topic}
你的身份：{d.name}
你的阵营：{d.side}
你要捍卫的立场：{d.stance or d.side}
对方阵营：{opp}
{team_line}
{persona}{rules}{methodology_sec}{func_sec}{brief_sec}{manifest_sec}## 比赛与发言规则
- 语速约定：1 分钟 ≈ {cfg.wpm} 字。每次发言会给出本轮字数上限，请勿超出。
- 这是**实时口语**辩论：直接说出你要讲的话，像真人现场发言。
- **只说话，严禁旁白**：不要任何神态/动作/舞台提示，不要括号内旁白，不要 Markdown 标题或列表符号。
- 紧扣对方刚说的话进行回应与反驳，同时推进己方论证；自信、有礼、逻辑严密、论据具体。
- 全部用中文（除非辩题/规则另有要求）。
- 你只是辩手，不是主持人：不要宣布环节、不要报时、不要替主持人串场。

请始终记住自己的立场与身份，全力以赴赢下这场辩论。"""


def build_traditional_prep_system_prompt(
    team: str,
    position: str,
    folder: Path,
    cfg: "LiveConfig",
    teammates: list[str],
) -> str:
    """阵营级完整备赛 system prompt。"""
    rules = f"\n## 本场自定义规则\n{cfg.rules.strip()}\n" if cfg.rules.strip() else ""
    teammate_line = f"本阵营参辩者：{'、'.join(teammates)}\n" if teammates else ""
    methodology_sec = methodology.prompt_instructions("prep")
    return f"""你是一支辩论队的{team}成员，正在为一场重要的辩论赛做准备。

辩题：{cfg.topic}
你的持方：{position}
{teammate_line}{rules}
{methodology_sec}
## 备赛任务
1. 广泛搜集与辩题相关的学术资料、统计数据、案例、权威观点
2. 分析辩题的关键概念，明确己方论证范围
3. 构建核心论点体系（至少3个主要论点）
4. 预判对方可能提出的反驳，准备应对策略
5. 最终形成完整的立论框架

## 事实核查与时效性（重要！务必遵守）
- 凡引用事实、数据、人物近况、平台动态，必须先交叉核对（多源比对）再采用；不确定的不要当成定论。
- 你的既有印象很可能过时。能联网就查最新；不能确证的，宁可弱化措辞或舍弃，绝不武断断言。
- 所有举证须带「时效 + 出处」锚点，例如「截止 2026 年 6 月」「据 2025 年 X 月报道」。
  缺可靠时间锚点的数字/近况，标注"待核实"或不用；宁缺毋滥，不可编造或想当然。

## 资料存放规则
- 所有资料必须存放在 `{folder}` 文件夹内
- 在 `research/` 子目录中存放搜集到的研究资料
- 在 `argument_framework.md` 中撰写完整的立论框架
- 在 `debate_brief.md` 中保存最终备赛交接简报

## 路径限制（重要！）
- 你只能访问 `{folder}` 文件夹内的文件
- 绝对禁止访问任何其他路径的文件

## 工具使用
你可以使用任何可用的工具进行资料搜集和分析。

请全力以赴，为{team}的胜利做好最充分的准备。"""


def build_moderator_system_prompt(cfg: "LiveConfig") -> str:
    """Build neutral moderator instructions backed by shared methodology."""
    methodology_sec = methodology.prompt_instructions("moderator")
    return (
        "你是一场实时辩论的主审，立场中立，不替任何一方说话。\n"
        f"辩题：{cfg.topic}\n"
        f"{methodology_sec}"
        "每个完整环节结束后，我会把该环节全部发言发给你。你要犀利、精炼地点评：指出其回避了什么、"
        "哪些例子不切题或以例代证、是否原地打转或转移焦点，并把讨论逼向更深一层"
        "（机制/前提/代价/边界/反例）。不复述发言、不和稀泥、不评判输赢。"
        "初始化读完方法论后，点评阶段不再调用工具。每条点评硬性控制在 100 字以内、能短则短。"
    )


def build_traditional_prep_tasks(
    team: str,
    position: str,
    opponent_position: str,
) -> list[tuple[str, str]]:
    """完整备赛的四个阶段任务。"""
    return [
        (
            "辩题分析",
            f"请先分析辩题「{position}」。需要完成：\n"
            f"1. 定义辩题中的关键概念\n"
            f"2. 明确论证范围和判准\n"
            f"3. 分析对方持方「{opponent_position}」可能的核心论点\n"
            f"将分析结果保存到 `research/topic_analysis.md`。",
        ),
        (
            "资料搜集",
            "广泛搜集与辩题相关的资料：\n"
            "1. 学术论文和研究报告\n"
            "2. 权威统计数据\n"
            "3. 经典案例和时事案例\n"
            "4. 专家观点和名人名言\n"
            "将搜集结果保存到 `research/data.md`。",
        ),
        (
            "论点构建",
            f"基于前期分析，构建{team}的论证体系：\n"
            "1. 提出至少3个核心论点\n"
            "2. 每个论点附完整的论证链（前提→推理→结论）\n"
            "3. 为每个论点配备2-3条支持性论据\n"
            "将论点体系保存到 `research/arguments.md`。",
        ),
        (
            "攻防演练",
            "预判对方可能的攻击方向：\n"
            "1. 列出对方最可能使用的3个攻击角度\n"
            "2. 针对每个攻击角度准备反驳策略\n"
            "3. 准备对对方立论的质询问题清单\n"
            "将结果保存到 `research/rebuttals.md`。",
        ),
    ]


def build_traditional_prep_final_prompt(team: str, position: str) -> str:
    """完整备赛的最终交接简报任务。"""
    return f"""备赛时间即将结束，请完成两件事：

1. 确认研究资料已分类保存到本方文件夹（research/ 下各 .md，及 argument_framework.md 立论框架），供场上随时 read 查阅。

2. 输出【备赛交接简报】——它将注入本阵营全部 AI 辩手的场上设定，必须精炼、可直接引用、{TRADITIONAL_PREP_BRIEF_MAX_CHARS}字以内，严格按以下结构：

【{team}备赛交接简报】
一、持方与定义：{position}；关键概念如何界定（争取对己方有利的定义）
二、核心论点（3条；每条 = 一句话主张 + 最有力的1个论据/数据/案例）
三、对方主攻方向预判（2-3条）及我方反驳要点（各一句）
四、质询要点（准备问对方的2-3个关键问题）
五、价值升华（一句话，留给总结收尾用）

只输出这份简报正文，不加任何额外说明或客套。"""


def build_turn_prompt(d: "DebaterCfg", cfg: "LiveConfig", phase_label: str,
                      max_chars: int, new_context: str, kind: str = "statement",
                      hint: str = "", respondent_name: str = "",
                      question_text: str = "", func: str = "") -> str:
    """构建一次发言的 prompt：按功能（开篇/延续/小结/总结/质询/接质/自由）给出不同要求。"""
    if kind == "cross_q":
        ask = (
            f"现在是【{phase_label}】，你正在质询 {respondent_name or '对方'}。"
            "针对其论证中的漏洞、事实错误或矛盾，提出一个简短、犀利、可追问的问题。"
            "只问一个问题，不要长篇大论、不要自己作答。"
        )
    elif kind == "cross_a":
        q = f"\n对方质询你：\n「{question_text}」\n" if question_text.strip() else ""
        ask = (
            f"现在是【{phase_label}】。{q}请直接、简洁地正面作答，不要反问、不要回避、不要长篇铺垫。"
        )
    elif kind == "free":
        ask = (
            f"现在是【{phase_label}】，双方交替发言。请先回应对方上一句，再推进己方论证；"
            "短促有力、直击要害，不要原地打转。"
        )
    else:  # statement：按功能给出对应要求（开篇/延续/小结/总结）
        base = _STATEMENT_ASK.get(func, "请围绕本环节要求清晰陈述：立场、核心论点与最有力的论据。")
        guide = f"{base} {hint.strip()}" if hint.strip() else base
        ask = f"现在是【{phase_label}】。{guide}"

    ctx = f"\n【你尚未回应的最新发言】\n{new_context}\n" if new_context.strip() else ""

    return (
        f"辩题：{cfg.topic}\n"
        f"你是 {d.name}（{d.side}）。\n"
        f"{ctx}\n"
        f"{ask}\n"
        f"字数上限：{max_chars} 字（约 {max(1, round(max_chars / max(1, cfg.wpm), 1))} 分钟），"
        f"请勿超出，也不要凑字数。直接输出你要说的话："
    )


def build_verify_prompt(text: str, max_chars: int) -> str:
    """「简洁复核」开关：把刚生成的发言复核并压缩为更紧凑、无废话的一段。"""
    return (
        "下面是你刚才的辩论发言草稿。请做一次自我复核：删去重复、口水话、空泛排比与跑题内容，"
        "只保留最有力的论点、论据与对对方的精准回应，让它更紧凑有力。"
        f"保持同一立场与口吻，输出一段流畅完整的中文，控制在 {max_chars} 字以内。"
        "直接输出复核后的发言本身，不要任何解释、标题或前后缀。\n\n"
        f"草稿：\n---\n{text}\n---\n\n复核后："
    )
