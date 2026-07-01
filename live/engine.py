"""实时真人 ⇄ AI 辩论编排引擎。

设计目标：任意人数、任意阵营、任意赛制（自定义规则），真人与 AI 混合参赛。
赛制由配置动态生成，发言可来自 AI（opencode）或真人（浏览器麦克风 STT /
打字，经 HTTP 提交）。

线程模型：一场辩论在后台线程里跑 run()；轮到真人发言时阻塞在 threading.Event 上，
直到 HTTP 层调用 submit_human() 喂入文本。所有进展通过 EventBroker 推给 SSE 订阅者。
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from debate_framework.opencode_runner import (
    OpenCodeRunner,
    generate_session_id,
    truncate_at_sentence,
)
from . import methodology
from . import prompts
from . import tts
from . import tts_live

logger = logging.getLogger(__name__)

# UI 下拉可选项（免费 opencode 模型 + 思考强度）。模型名按 opencode provider 习惯。
MODEL_CHOICES: list[dict[str, str]] = [
    {"value": "opencode/mimo-v2.5-free", "name": "MiMo v2.5 (free)"},
    {"value": "opencode/deepseek-flash", "name": "DeepSeek Flash (free)"},
    {"value": "opencode/grok-code-fast", "name": "Grok Code Fast (free)"},
    {"value": "opencode/qwen3-coder-free", "name": "Qwen3 Coder (free)"},
    {"value": "claude-sonnet-4-5-20250929", "name": "Claude Sonnet 4.5"},
]
THINKING_CHOICES: list[str] = ["none", "low", "medium", "high"]

# 各类发言默认时长（分钟）→ 与 wpm 相乘得本轮字数上限。
DEFAULT_OPENING_MIN = 2.0
DEFAULT_FREE_MIN = 1.0
DEFAULT_CLOSING_MIN = 2.0

# 真人发言最长等待（秒）。超时视为弃权，流程继续。
HUMAN_TURN_TIMEOUT = 1800


def _safe_path_part(text: str) -> str:
    """Make a readable path segment from a user-facing side name."""
    s = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", (text or "").strip(), flags=re.UNICODE)
    return s.strip("._")[:40] or "side"


@dataclass
class DebaterCfg:
    """单个辩手配置（真人或 AI）。"""
    name: str
    side: str                       # 阵营名，任意字符串（如「正方」「红队」「张三方」）
    kind: str = "ai"                # "ai" | "human"
    stance: str = ""                # 该辩手要捍卫的具体立场（空则用 side）
    model: str = "opencode/mimo-v2.5-free"
    thinking: str = "medium"
    voice: str = tts_live.DEFAULT_VOICE
    persona: str = ""               # 可选人设/口吻
    custom_prompt: str = ""         # 直接给定的整段 system prompt（覆盖自动生成）
    brief: str = ""                 # 已保存的备赛简报（从库加载时填充 → 可跳过备赛直接上场）
    seat: int = 0                   # 同阵营内座位号（1-based）；赛制模块据 (side,seat) 引用辩手
    id: str = field(default_factory=lambda: "d_" + uuid.uuid4().hex[:8])


@dataclass
class LiveConfig:
    """一场实时辩论的全局配置。"""
    topic: str
    debaters: list[DebaterCfg]
    rules: str = ""
    wpm: int = 240
    free_rounds: int = 3
    opening_minutes: float = DEFAULT_OPENING_MIN
    free_minutes: float = DEFAULT_FREE_MIN
    closing_minutes: float = DEFAULT_CLOSING_MIN
    use_moderator: bool = False
    moderator_model: str = "opencode/mimo-v2.5-free"
    moderator_thinking: str = "medium"
    double_check: bool = False      # 简洁复核：每条 AI 发言后再复核压缩（更慢）
    manual_advance: bool = True     # AI 逐句手动推进：每句 AI 发言前等待用户点击「下一句」
    # 语音引擎："browser"=浏览器内置语音合成（零延迟、离线、最快，默认）；
    #           "edge"=edge-tts 在线高质量（后台异步合成，作为可选/回退）。
    tts_engine: str = "browser"
    # STT 辅助关键词：前端用于 Web Speech contextual biasing（浏览器支持时）。
    stt_keywords: list[str] = field(default_factory=list)
    # STT 关键词提供方 id（插件化；默认 opencode）。
    stt_provider: str = "opencode"
    # 模块化赛制：有序模块列表，每项形如
    #   {"type":"statement","minutes":2,"label":"开场申论","side":"正方","seat":1,"hint":"..."}
    #   {"type":"cross_exam","minutes":2,"label":"质询","questioners":[["反方",4]],
    #    "respondents":[["正方",1]],"single_side":true}
    #   {"type":"free_debate","minutes":4,"label":"自由辩论"}
    # 为空时回退到「开场陈词 / 自由辩论×free_rounds / 总结陈词」的默认赛制。
    modules: list = field(default_factory=list)


@dataclass
class PhaseSpec:
    """动态生成的一个发言位。"""
    label: str
    debater_id: str
    max_chars: int
    kind: str          # "statement" | "cross_q" | "cross_a" | "free"
    respondent_id: str = ""   # 质询：cross_q 指向被质询人；cross_a 指向质询人
    hint: str = ""            # 申论/陈词的内容提示
    timed: bool = True        # 是否计时（质询单边计时下，被质询方作答 timed=False）
    func: str = ""            # 功能键（statement：opening/continuation/cross_summary/closing）
    review_group: str = ""    # 同一完整环节共享分组；为空表示该发言本身就是完整环节
    review_label: str = ""    # 主审点评时使用的完整环节名


# ---------------------------------------------------------------------------
# 事件广播：线程安全的发布/订阅 + 历史回放（供 SSE 与晚到的订阅者补齐）
# ---------------------------------------------------------------------------
class EventBroker:
    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._history: list[dict] = []
        self._lock = threading.Lock()

    def publish(self, event: dict) -> dict:
        with self._lock:
            event = {**event, "seq": len(self._history), "t": time.time()}
            self._history.append(event)
            for q in list(self._subs):
                q.put(event)
        return event

    def subscribe(self, since_seq: int = -1) -> queue.Queue:
        """订阅事件流。since_seq>=0 时只回放 seq>since_seq 的历史（用于 SSE 断线重连去重）。"""
        q: queue.Queue = queue.Queue()
        with self._lock:
            for e in self._history:     # 回放历史，使新订阅者立即看到（断线重连只补未读）
                if e["seq"] > since_seq:
                    q.put(e)
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def history(self) -> list[dict]:
        with self._lock:
            return list(self._history)


# ---------------------------------------------------------------------------
# 实时辩论
# ---------------------------------------------------------------------------
class LiveDebate:
    """一场实时辩论的状态机与编排器。"""

    def __init__(self, cfg: LiveConfig, work_dir: Path,
                 broker: Optional[EventBroker] = None,
                 entry_id: Optional[str] = None) -> None:
        self.cfg = cfg
        self.entry_id = entry_id      # 库条目 id（用于保存备赛/记录）；None 则不落库
        self.work_dir = Path(work_dir)
        self.audio_dir = self.work_dir / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.broker = broker or EventBroker()

        self.state: str = "idle"        # idle|preparing|prepared|running|done|stopped|error
        self.transcript: list[dict] = []
        self.mic_on: bool = True

        # 每个 AI 辩手独立 runner / session / 资料目录 / 上下文游标。
        self._runners: dict[str, OpenCodeRunner] = {}
        self._sessions: dict[str, str] = {}
        self._folders: dict[str, Path] = {}
        self._side_folders: dict[str, Path] = {}
        self._cursor: dict[str, int] = {}
        self._inited: set[str] = set()
        sides: list[str] = []
        for d in cfg.debaters:
            if d.side not in sides:
                sides.append(d.side)
        for idx, side in enumerate(sides, 1):
            folder = self.work_dir / "sides" / f"{idx}_{_safe_path_part(side)}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "research").mkdir(parents=True, exist_ok=True)
            methodology.install_into(folder)
            self._side_folders[side] = folder
        for d in cfg.debaters:
            if d.kind == "ai":
                self._runners[d.id] = OpenCodeRunner(model=d.model, timeout=180)
                self._sessions[d.id] = generate_session_id(f"live-{d.id}")
                folder = self._side_folders[d.side]
                self._folders[d.id] = folder

        # 主审（可选）
        self._mod_runner: Optional[OpenCodeRunner] = None
        self._mod_session: str = ""

        # 真人发言同步
        self._human_lock = threading.Lock()
        self._human_event = threading.Event()
        self._human_text: str = ""
        self._awaiting_id: str = ""

        # AI 逐句手动推进：每句 AI 发言前阻塞，等待 advance()（点击「下一句」）。
        self._advance_event = threading.Event()
        self._awaiting_advance: str = ""
        self._last_q: str = ""          # 最近一次质询提问文本（供作答方组织回答）
        self._func_map = None           # {debater_id: [功能键]}，懒计算并缓存

        self._stop = threading.Event()
        # 暂停闸：set=继续运行，clear=暂停（在每个发言位之间阻塞，不打断进行中的一次调用）。
        self._resume = threading.Event()
        self._resume.set()
        self._thread: Optional[threading.Thread] = None
        self._prep_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def _by_id(self, did: str) -> DebaterCfg:
        for d in self.cfg.debaters:
            if d.id == did:
                return d
        raise KeyError(did)

    def _sides(self) -> list[str]:
        seen: list[str] = []
        for d in self.cfg.debaters:
            if d.side not in seen:
                seen.append(d.side)
        return seen

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "topic": self.cfg.topic,
            "mic_on": self.mic_on,
            "paused": not self._resume.is_set(),
            "manual_advance": self.cfg.manual_advance,
            "awaiting_id": self._awaiting_id,
            "awaiting_advance": self._awaiting_advance,
            "debaters": [
                {"id": d.id, "name": d.name, "side": d.side, "kind": d.kind,
                 "model": d.model, "voice": d.voice}
                for d in self.cfg.debaters
            ],
            "turns": len(self.transcript),
        }

    def briefs(self) -> list[dict]:
        """当前各 AI 辩手的备赛简报（用于「备赛材料」子标签查看）。"""
        return [
            {"id": d.id, "name": d.name, "side": d.side, "brief": d.brief}
            for d in self.cfg.debaters if d.kind == "ai"
        ]

    # ------------------------------------------------------------------
    # 阶段一：备赛（点击触发，后台执行）
    # ------------------------------------------------------------------
    def prepare(self) -> None:
        """为每个含 AI 的阵营做完整赛前备赛（真人无需备赛）。"""
        if self.state in ("preparing", "running"):
            return
        self.state = "preparing"
        self.broker.publish({"type": "status", "state": "preparing"})
        self._prep_thread = threading.Thread(target=self._prepare_worker, name="live-prepare", daemon=True)
        self._prep_thread.start()

    def _prepare_worker(self) -> None:
        try:
            ai_by_side = self._ai_by_side()
            for side, members in ai_by_side.items():
                if self._stop.is_set():
                    break
                lead = self._prep_lead(members)
                self.broker.publish({"type": "prep_start", "debater": lead.id, "name": f"{side}全队"})
                brief = self._run_traditional_prep_for_side(side, members)
                ok = bool(brief) and not brief.startswith("[调用错误")
                for d in members:
                    if ok:
                        d.brief = brief
                        self._save_brief(d)
                    self.broker.publish({"type": "prep_done", "debater": d.id,
                                         "name": d.name, "ok": ok,
                                         "brief": brief[:1200] if ok else ""})
            if self._mod_needed():
                self._setup_moderator()
            # 若期间用户已点「开始」（state 变为 running），不要把状态退回 prepared
            if self.state == "preparing":
                self.state = "prepared"
            self.broker.publish({"type": "status", "state": "prepared"})
        except Exception as e:
            logger.exception("备赛失败")
            self.state = "error"
            self.broker.publish({"type": "error", "where": "prepare", "msg": str(e)})

    def _ai_by_side(self) -> dict[str, list[DebaterCfg]]:
        out: dict[str, list[DebaterCfg]] = {}
        for d in self.cfg.debaters:
            if d.kind == "ai":
                out.setdefault(d.side, []).append(d)
        for members in out.values():
            members.sort(key=lambda d: (d.seat, d.name))
        return out

    def _prep_lead(self, members: list[DebaterCfg]) -> DebaterCfg:
        """Use the AI with the largest seat number as the team's prep runner/model."""
        return max(members, key=lambda d: (d.seat, d.name))

    def _side_position(self, side: str) -> str:
        members = [d for d in self.cfg.debaters if d.side == side]
        stances: list[str] = []
        named: list[str] = []
        for d in members:
            stance = d.stance.strip()
            if not stance:
                continue
            if stance not in stances:
                stances.append(stance)
            named.append(f"{d.name}：{stance}")
        if len(stances) == 1:
            return stances[0]
        if named:
            return "；".join(named)
        return side

    def _opponent_position(self, side: str) -> str:
        positions = [self._side_position(s) for s in self._sides() if s != side]
        return "；".join(positions) if positions else "对方"

    def _run_traditional_prep_for_side(self, side: str, members: list[DebaterCfg]) -> str:
        """Run the old multi-step prep flow once per side and return the handoff brief."""
        lead = self._prep_lead(members)
        folder = self._folders[lead.id]
        (folder / "research").mkdir(parents=True, exist_ok=True)
        runner = self._runners[lead.id]
        session_id = generate_session_id(f"prep-{_safe_path_part(side)}")
        position = self._side_position(side)
        opponent_position = self._opponent_position(side)
        teammates = [d.name for d in self.cfg.debaters if d.side == side]

        system_prompt = prompts.build_traditional_prep_system_prompt(
            team=side,
            position=position,
            folder=folder,
            cfg=self.cfg,
            teammates=teammates,
        )
        init_msg = (
            f"[系统设定]\n{system_prompt}\n\n"
            "请先完整读取两份共享方法论，再回复「已就绪，开始资料搜集。」"
        )
        runner.run_safe(
            message=init_msg,
            session_id=session_id,
            title=f"{side}备赛",
            model=lead.model,
            cwd=folder,
            pure=True,
            variant=self._variant(lead.thinking),
        )

        for task_name, task_prompt in prompts.build_traditional_prep_tasks(
            team=side,
            position=position,
            opponent_position=opponent_position,
        ):
            logger.info("[%s] 执行传统备赛任务: %s", side, task_name)
            self.broker.publish({"type": "host", "text": f"（{side}传统备赛：{task_name}）"})
            runner.run_safe(
                message=task_prompt,
                session_id=session_id,
                title=f"{side}备赛",
                model=lead.model,
                cwd=folder,
                pure=True,
                variant=self._variant(lead.thinking),
            )

        final = runner.run_safe(
            message=prompts.build_traditional_prep_final_prompt(side, position),
            session_id=session_id,
            title=f"{side}备赛总结",
            model=lead.model,
            cwd=folder,
            pure=True,
            variant=self._variant(lead.thinking),
        )
        brief = truncate_at_sentence(
            (final or "").strip(),
            prompts.TRADITIONAL_PREP_BRIEF_MAX_CHARS,
        )
        if brief and not brief.startswith("[调用错误"):
            try:
                (folder / "debate_brief.md").write_text(brief, encoding="utf-8")
            except OSError as e:
                logger.warning("[%s] 写入 debate_brief.md 失败: %s", side, e)
        logger.info("[%s] 传统备赛完成，交接简报 %d 字", side, len(brief))
        return brief

    def mark_loaded(self) -> None:
        """从库加载（含已保存简报）后调用：标记备赛就绪，使界面可直接「开始」跳过备赛。"""
        self.state = "prepared"
        for d in self.cfg.debaters:
            if d.kind == "ai" and d.brief:
                self.broker.publish({"type": "prep_done", "debater": d.id,
                                     "name": d.name, "ok": True,
                                     "brief": d.brief[:1200], "loaded": True})
        self.broker.publish({"type": "status", "state": "prepared"})

    def _ensure_session(self, d: DebaterCfg) -> None:
        """首次使用某 AI 辩手前，把 system prompt（含已有备赛简报，如有）注入其会话。"""
        if d.id in self._inited:
            return
        if not hasattr(self, "_func_map") or self._func_map is None:
            self._func_map = self._functions_for_debaters()
        sysp = prompts.build_system_prompt(
            d, self.cfg,
            opponents=[s for s in self._sides() if s != d.side],
            teammates=[x.name for x in self.cfg.debaters if x.side == d.side],
            brief=d.brief,
            functions=self._func_map.get(d.id, []),
            prep_manifest=self._prep_manifest(d),
        )
        self._runners[d.id].run_safe(
            message=(
                f"[系统设定]\n{sysp}\n\n"
                f"请先完整读取两份共享方法论，再回复「{d.name}已就位」。"
            ),
            session_id=self._sessions[d.id], title=d.name,
            model=d.model, cwd=self._folders[d.id], pure=True,
            variant=self._variant(d.thinking),
        )
        self._inited.add(d.id)

    def _prep_manifest(self, d: DebaterCfg) -> str:
        """List traditional prep files available in this AI debater's side folder."""
        folder = self._folders.get(d.id)
        if not folder:
            return ""
        files: list[Path] = []
        for pattern in ("*.md", "research/*.md", "methodology/*.md"):
            files.extend(sorted(folder.glob(pattern)))
        lines: list[str] = []
        for f in files:
            if not f.is_file():
                continue
            try:
                rel = f.relative_to(folder).as_posix()
            except ValueError:
                continue
            if rel == "debate_brief.md":
                desc = "备赛交接简报"
            elif rel == "argument_framework.md":
                desc = "完整立论框架"
            elif rel.endswith("topic_analysis.md"):
                desc = "辩题分析与定义判准"
            elif rel.endswith("data.md"):
                desc = "资料、数据、案例与来源"
            elif rel.endswith("arguments.md"):
                desc = "核心论点与论证链"
            elif rel.endswith("rebuttals.md"):
                desc = "攻防预案与质询问题"
            elif rel.startswith("methodology/"):
                desc = "双方与主审共用的辩论方法论"
            else:
                desc = "传统备赛资料"
            lines.append(f"- `{rel}` — {desc}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 落库（保存备赛 / 配置 / 记录）
    # ------------------------------------------------------------------
    def to_meta(self) -> dict:
        c = self.cfg
        return {
            "topic": c.topic, "rules": c.rules, "wpm": c.wpm,
            "free_rounds": c.free_rounds, "opening_minutes": c.opening_minutes,
            "free_minutes": c.free_minutes, "closing_minutes": c.closing_minutes,
            "use_moderator": c.use_moderator, "moderator_model": c.moderator_model,
            "moderator_thinking": c.moderator_thinking, "double_check": c.double_check,
            "manual_advance": c.manual_advance, "tts_engine": c.tts_engine,
            "stt_keywords": c.stt_keywords, "stt_provider": c.stt_provider,
            "modules": c.modules,
            "debaters": [
                {"id": d.id, "name": d.name, "side": d.side, "kind": d.kind, "seat": d.seat,
                 "stance": d.stance, "model": d.model, "thinking": d.thinking,
                 "voice": d.voice, "persona": d.persona, "custom_prompt": d.custom_prompt}
                for d in c.debaters
            ],
        }

    def save_meta(self) -> None:
        if not self.entry_id:
            return
        try:
            from . import library
            library.save_meta(self.entry_id, self.to_meta())
        except Exception as e:
            logger.warning("保存配置失败: %s", e)

    def _save_brief(self, d: DebaterCfg) -> None:
        if not self.entry_id:
            return
        try:
            from . import library
            library.save_brief(self.entry_id, d.id, d.brief)
        except Exception as e:
            logger.warning("保存备赛简报失败: %s", e)

    def _save_transcript(self) -> None:
        if not self.entry_id:
            return
        try:
            from . import library
            library.save_transcript(self.entry_id, self.transcript)
        except Exception as e:
            logger.warning("保存记录失败: %s", e)

    # ------------------------------------------------------------------
    # 阶段二：实时比赛（点击触发，后台执行）
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self.state == "running":
            return
        self._stop.clear()
        self.state = "running"
        self.broker.publish({"type": "status", "state": "running"})
        self._thread = threading.Thread(target=self._run_worker, name="live-run", daemon=True)
        self._thread.start()

    def _run_worker(self) -> None:
        try:
            # 若用户在备赛尚未结束时就点了「开始」，先等备赛线程收尾，避免两线程并发
            # 重复初始化同一辩手会话 / 抢占 opencode。
            if self._prep_thread and self._prep_thread.is_alive():
                self.broker.publish({"type": "host", "text": "（等待 AI 备赛完成后开始……）"})
                self._prep_thread.join()
            # 跳过备赛直接开始时，主审尚未建立 → 此处补建，确保开关真正生效。
            if self._mod_needed() and not self._mod_runner:
                try:
                    self._setup_moderator()
                except Exception as e:
                    logger.warning("主审初始化失败，本场无主审: %s", e)
            phases = self._build_phases()
            self.broker.publish({"type": "begin", "total": len(phases),
                                 "topic": self.cfg.topic})
            self._host(f"本场辩论开始。辩题：{self.cfg.topic}。"
                       f"参辩 {len(self.cfg.debaters)} 位，先进行开场陈词。")
            review_group = ""
            review_speeches: list[tuple[DebaterCfg, PhaseSpec, str]] = []
            for i, ph in enumerate(phases):
                if self._stop.is_set():
                    break
                # 暂停闸：在发言位之间阻塞，直到 resume（或 stop）。不打断进行中的调用。
                if not self._resume.is_set():
                    self.broker.publish({"type": "status", "state": "paused"})
                    self._resume.wait()
                    if self._stop.is_set():
                        break
                    self.broker.publish({"type": "status", "state": "running"})
                d = self._by_id(ph.debater_id)
                self.broker.publish({"type": "phase", "idx": i, "total": len(phases),
                                     "label": ph.label, "debater": d.id,
                                     "name": d.name, "side": d.side, "kind": d.kind})
                if d.kind == "human":
                    text = self._human_turn(d, ph)
                else:
                    # 逐句手动推进：本句 AI 发言前等待用户点击「下一句」（无点击不前进）。
                    if self.cfg.manual_advance:
                        self._await_advance(d, ph)
                        if self._stop.is_set():
                            break
                    text = self._ai_turn(d, ph)
                if self._stop.is_set():
                    break
                if ph.kind == "cross_q":        # 记下提问，供随后的作答方组织回答
                    self._last_q = text
                if self.cfg.use_moderator:
                    if ph.review_group:
                        # 质询、自由辩论和对称陈词会展开成多个发言位，但主审只在完整环节
                        # 全部结束后，汇总其中所有发言统一点评一次。
                        if review_group != ph.review_group:
                            review_group = ph.review_group
                            review_speeches = []
                        if text.strip():
                            review_speeches.append((d, ph, text))
                        next_group = (phases[i + 1].review_group
                                      if i + 1 < len(phases) else "")
                        if next_group != ph.review_group:
                            if review_speeches:
                                self._moderator_review_phase(
                                    ph.review_label or ph.label,
                                    review_speeches,
                                )
                            review_group = ""
                            review_speeches = []
                    elif text.strip():
                        self._moderator_review(d, ph, text)
            if self._stop.is_set():
                self.state = "stopped"
                self._host("辩论已停止。")
                self.broker.publish({"type": "status", "state": "stopped"})
            else:
                self.state = "done"
                self._host("全部环节结束，本场辩论到此结束，感谢各位。")
                self.broker.publish({"type": "status", "state": "done"})
            self._save_transcript()       # 落库：本场逐条发言记录
            self.broker.publish({"type": "done"})
        except Exception as e:
            logger.exception("比赛执行失败")
            self.state = "error"
            self.broker.publish({"type": "error", "where": "run", "msg": str(e)})

    def _build_phases(self) -> list[PhaseSpec]:
        if self.cfg.modules:
            return self._expand_modules()
        return self._default_phases()

    def _default_phases(self) -> list[PhaseSpec]:
        """无模块化赛制时的默认：开场陈词 / 自由辩论×free_rounds / 总结陈词。"""
        c = self.cfg
        open_max = max(40, int(c.wpm * c.opening_minutes))
        free_max = max(30, int(c.wpm * c.free_minutes))
        close_max = max(40, int(c.wpm * c.closing_minutes))
        phases: list[PhaseSpec] = []
        for d in c.debaters:
            phases.append(PhaseSpec(
                "开场陈词", d.id, open_max, "statement",
                hint="开场立论：界定立场、亮出核心论点与最有力论据。",
                func="opening", review_group="default:opening",
                review_label="开场陈词",
            ))
        # 自由辩论：双方严格交替（_free_order 保证），共 free_rounds×人数 次。
        for i, d in enumerate(self._free_order(c.free_rounds * len(c.debaters))):
            phases.append(PhaseSpec(
                f"自由辩论 #{i + 1}", d.id, free_max, "free",
                review_group="default:free_debate", review_label="自由辩论",
            ))
        for d in c.debaters:
            phases.append(PhaseSpec(
                "总结陈词", d.id, close_max, "statement",
                hint="总结陈词：回应全场交锋、重申己方为何成立、升华价值收尾。",
                func="closing", review_group="default:closing",
                review_label="总结陈词",
            ))
        return phases

    def _functions_for_debaters(self) -> dict[str, list[str]]:
        """据赛制模块推导每位辩手承担的功能键列表（保持出现顺序，去重）。

        statement→其 function；cross_exam→质询方 'question'、被质询方 'answer'；
        free_debate→全体 'free'。无模块时回退到默认赛制的功能（开篇/自由/总结）。
        """
        out: dict[str, list[str]] = {d.id: [] for d in self.cfg.debaters}

        def add(did: str, fk: str) -> None:
            if did and did in out and fk not in out[did]:
                out[did].append(fk)

        if not self.cfg.modules:
            # 默认赛制：每人 开篇申论 + 自由辩论 + 总结陈词。
            for d in self.cfg.debaters:
                add(d.id, "opening"); add(d.id, "free"); add(d.id, "closing")
            return out

        for m in self.cfg.modules:
            typ = m.get("type")
            if typ == "statement":
                did = self._resolve_seat(m.get("side", ""), int(m.get("seat", 1) or 1))
                add(did, m.get("function", "opening"))
            elif typ == "cross_exam":
                for s, n in m.get("questioners", []):
                    add(self._resolve_seat(s, int(n)), "question")
                for s, n in m.get("respondents", []):
                    add(self._resolve_seat(s, int(n)), "answer")
            elif typ == "free_debate":
                for d in self.cfg.debaters:
                    add(d.id, "free")
        return out

    def _resolve_seat(self, side: str, seat: int) -> str:
        """把 (阵营, 座位号) 解析为辩手 id；缺该座位则回退到同阵营首位、再回退任意。"""
        for d in self.cfg.debaters:
            if d.side == side and d.seat == seat:
                return d.id
        for d in self.cfg.debaters:
            if d.side == side:
                return d.id
        return self.cfg.debaters[0].id if self.cfg.debaters else ""

    def _expand_modules(self) -> list[PhaseSpec]:
        """把模块化赛制展开为有序发言位列表。"""
        c = self.cfg
        wpm = c.wpm
        phases: list[PhaseSpec] = []
        # UI 中的“镜像申论/陈词”：相邻、功能相同、阵营相反的两个 statement
        # 视为一个对称环节。主审须等双方都说完，再汇总点评一次。
        statement_review_groups: dict[int, tuple[str, str]] = {}
        module_index = 0
        while module_index + 1 < len(c.modules):
            first = c.modules[module_index]
            second = c.modules[module_index + 1]
            first_func = first.get("function", "opening")
            second_func = second.get("function", "opening")
            if (
                first.get("type") == second.get("type") == "statement"
                and first_func == second_func
                and first.get("side", "") != second.get("side", "")
            ):
                from .prompts import FUNCTION_LABELS
                review_group = f"modules:{module_index}-{module_index + 1}:statement"
                review_label = (
                    (first.get("label") or "").strip()
                    or (second.get("label") or "").strip()
                    or FUNCTION_LABELS.get(first_func, "申论/陈词")
                )
                statement_review_groups[module_index] = (review_group, review_label)
                statement_review_groups[module_index + 1] = (review_group, review_label)
                module_index += 2
            else:
                module_index += 1

        for module_index, m in enumerate(c.modules):
            typ = m.get("type")
            minutes = float(m.get("minutes", 2) or 2)
            label = (m.get("label") or "").strip()
            if typ == "statement":
                did = self._resolve_seat(m.get("side", ""), int(m.get("seat", 1) or 1))
                if not did:
                    continue
                func = m.get("function", "opening")
                from .prompts import FUNCTION_LABELS
                review_group, review_label = statement_review_groups.get(
                    module_index, ("", "")
                )
                phases.append(PhaseSpec(label or FUNCTION_LABELS.get(func, "申论"), did,
                                        max(40, round(minutes * wpm)), "statement",
                                        hint=m.get("hint", ""), func=func,
                                        review_group=review_group,
                                        review_label=review_label))
            elif typ == "cross_exam":
                qs = [self._resolve_seat(s, int(n)) for s, n in m.get("questioners", [])]
                rs = [self._resolve_seat(s, int(n)) for s, n in m.get("respondents", [])]
                qs = [x for x in qs if x]
                rs = [x for x in rs if x]
                if not qs or not rs:
                    continue
                single = bool(m.get("single_side", True))
                # 互动环节按半速估算轮数：每轮 ≈ 提问+作答；提问/作答各约 25 秒额度。
                rounds = max(1, round(minutes / 0.8))
                per_q = max(20, round(wpm * 0.42))
                per_a = max(20, round(wpm * 0.42))
                base = label or "质询"
                review_group = f"module:{module_index}:cross_exam"
                for r in range(rounds):
                    q = qs[r % len(qs)]
                    resp = rs[r % len(rs)]
                    phases.append(PhaseSpec(f"{base} · 提问", q, per_q, "cross_q",
                                            respondent_id=resp, timed=True,
                                            review_group=review_group,
                                            review_label=base))
                    # 单边计时：被质询方作答不计时（timed=False → 真人无倒计时）。
                    phases.append(PhaseSpec(f"{base} · 作答", resp, per_a, "cross_a",
                                            respondent_id=q, timed=not single,
                                            review_group=review_group,
                                            review_label=base))
            elif typ == "free_debate":
                # 硬性要求：双方【严格交替】发言；AI 每次发言字数 ≈ 总时长 / 5~6。
                per = max(30, round(minutes * wpm / 5.5))
                total = max(2, round(minutes * wpm / per))     # ≈ 5~6 次
                # 可选 participants：[[阵营,座位],...]；缺省/非法 → 全体上场。
                refs = m.get("participants") or []
                pids = [self._resolve_seat(s, int(n)) for s, n in refs]
                order = self._free_order(total, [x for x in pids if x] or None)
                if not order:
                    continue
                base = label or "自由辩论"
                review_group = f"module:{module_index}:free_debate"
                for i, d in enumerate(order):
                    phases.append(PhaseSpec(
                        f"{base} #{i + 1}", d.id, per, "free",
                        review_group=review_group, review_label=base,
                    ))
        return phases

    def _free_order(self, total: int,
                    participant_ids: Optional[list[str]] = None) -> list[DebaterCfg]:
        """自由辩论发言顺序：硬性【双方严格交替】——逐回合在各阵营间轮换，
        阵营内按辩位轮流。即使两边人数不等也绝不连续同方发言。

        participant_ids 给定时仅这些辩手上场（可两边人数不等）；但若筛选后不足两个
        阵营或某阵营为空，则回退到全体——保证「至少每方一人」、对阵成立。"""
        pool = self.cfg.debaters
        if participant_ids:
            wanted = set(participant_ids)
            sel = [d for d in self.cfg.debaters if d.id in wanted]
            if len(sel) >= 2 and len({d.side for d in sel}) >= 2:
                pool = sel
        groups: dict[str, list[DebaterCfg]] = {}
        side_order: list[str] = []
        for d in pool:
            if d.side not in groups:
                groups[d.side] = []
                side_order.append(d.side)
            groups[d.side].append(d)
        if not side_order:
            return []
        cursor = {s: 0 for s in side_order}
        seq: list[DebaterCfg] = []
        for i in range(max(0, total)):
            side = side_order[i % len(side_order)]   # 严格按阵营顺序轮换
            grp = groups[side]
            d = grp[cursor[side] % len(grp)]
            cursor[side] += 1
            seq.append(d)
        return seq

    # ------------------------------------------------------------------
    # AI 发言
    # ------------------------------------------------------------------
    def _ai_turn(self, d: DebaterCfg, ph: PhaseSpec) -> str:
        self.broker.publish({"type": "turn_start", "debater": d.id, "name": d.name,
                             "side": d.side, "kind": "ai", "label": ph.label})
        # （此处不再阻塞——advance 闸已在 run 循环里、本句生成之前处理。）
        self._ensure_session(d)
        new_ctx = self._consume_new(d.name)
        respondent_name = ""
        if ph.respondent_id:
            try:
                respondent_name = self._by_id(ph.respondent_id).name
            except KeyError:
                respondent_name = "对方"
        prompt = prompts.build_turn_prompt(
            d, self.cfg, ph.label, ph.max_chars, new_ctx, kind=ph.kind, hint=ph.hint,
            respondent_name=respondent_name, question_text=self._last_question(), func=ph.func)
        text = self._runners[d.id].run_safe(
            message=prompt, session_id=self._sessions[d.id], title=d.name,
            model=d.model, cwd=self._folders[d.id], pure=True,
            variant=self._variant(d.thinking),
        )
        text = (text or "").strip()
        if text.startswith("[调用错误"):
            text = "（本轮发言生成失败，跳过。）"
        if self.cfg.double_check and not text.startswith("（"):
            v = self._runners[d.id].run_safe(
                message=prompts.build_verify_prompt(text, ph.max_chars),
                session_id=generate_session_id(f"verify-{d.id}"), title=f"{d.name}复核",
                model=d.model, cwd=self._folders[d.id], pure=True, variant="",
            )
            v = (v or "").strip()
            if v and not v.startswith("[调用错误"):
                text = v
        text = truncate_at_sentence(text, ph.max_chars)
        self._record(d.name, d.side, ph.label, text, "ai")

        # 关键：发言文本「立即」推送，不再等 TTS 合成（此前 edge-tts 阻塞拖慢全程）。
        tid = f"{d.id}_{len(self.transcript)}"
        eng = tts.get(self.cfg.tts_engine)
        server_tts = bool(eng and eng.server_side)
        self.broker.publish({"type": "turn", "debater": d.id, "name": d.name,
                             "side": d.side, "kind": "ai", "label": ph.label,
                             "text": text, "audio_url": "", "tid": tid,
                             "tts_engine": self.cfg.tts_engine,
                             "server_tts": server_tts, "voice": d.voice,
                             "char_count": len(text)})
        # 客户端引擎（如 browser）：前端用 speechSynthesis 即时朗读，服务端不合成（最快）。
        # 服务端引擎（如 edge 或自定义插件）：后台异步合成，完成后单独推 audio 事件补播——不阻塞发言显示。
        if server_tts:
            threading.Thread(target=self._synth_tts_async, args=(eng, d, text, tid),
                             name=f"tts-{tid}", daemon=True).start()
        return text

    def _synth_tts_async(self, eng: "tts.TTSEngine", d: DebaterCfg,
                         text: str, tid: str) -> None:
        """后台用所选服务端 TTS 引擎合成本句语音，完成后推 audio 事件让前端补播。"""
        try:
            af = self.audio_dir / f"{tid}.{getattr(eng, 'ext', 'mp3')}"
            if eng.synthesize(text, d.voice, af):
                self.broker.publish({"type": "audio", "tid": tid,
                                     "audio_url": f"/audio/{af.name}"})
        except Exception as e:
            logger.warning("TTS 后台合成失败 (%s/%s): %s", eng.id, tid, e)

    # ------------------------------------------------------------------
    # 逐句手动推进（AI 发言前等待点击「下一句」）
    # ------------------------------------------------------------------
    def _await_advance(self, d: DebaterCfg, ph: PhaseSpec) -> None:
        """阻塞，直到 advance()（用户点击「下一句」）或 stop()。无点击不前进。"""
        self._advance_event.clear()
        self._awaiting_advance = d.id
        self.broker.publish({"type": "await_advance", "debater": d.id, "name": d.name,
                             "side": d.side, "label": ph.label})
        self._advance_event.wait()
        self._awaiting_advance = ""

    def advance(self) -> bool:
        """HTTP 层调用：推进一句 AI 发言。仅在正等待推进时有效。"""
        if not self._awaiting_advance:
            return False
        self._advance_event.set()
        return True

    # ------------------------------------------------------------------
    # 真人发言（阻塞等待 HTTP 提交）
    # ------------------------------------------------------------------
    def _human_turn(self, d: DebaterCfg, ph: PhaseSpec) -> str:
        # 质询作答时把「对方的提问」作为上文展示；其余取对方最近发言。
        last_opp = self._last_q if ph.kind == "cross_a" else self._last_opponent_text(d.side)
        with self._human_lock:
            self._human_event.clear()
            self._human_text = ""
            self._awaiting_id = d.id
        # 真人发言时限（秒）：后端按真实时间强制推进，避免只靠前端倒计时。
        seconds = max(15, round(ph.max_chars / max(1, self.cfg.wpm) * 60))
        self.broker.publish({"type": "await_human", "debater": d.id, "name": d.name,
                             "side": d.side, "label": ph.label, "kind": ph.kind,
                             "max_chars": ph.max_chars, "mic_on": self.mic_on,
                             "seconds": seconds, "last_opponent": last_opp[:400]})
        got = self._human_event.wait(timeout=seconds)
        with self._human_lock:
            text = (self._human_text or "").strip()
            self._awaiting_id = ""
        if not got or not text:
            text = "（真人辩手未发言，本轮跳过。）"
        self._record(d.name, d.side, ph.label, text, "human")
        # 真人发言不合成语音（自己已说出）。
        self.broker.publish({"type": "turn", "debater": d.id, "name": d.name,
                             "side": d.side, "kind": "human", "label": ph.label,
                             "text": text, "audio_url": "", "char_count": len(text)})
        return text

    def submit_human(self, text: str) -> bool:
        """HTTP 层调用：喂入真人发言，解除阻塞。仅在正等待真人时有效。"""
        with self._human_lock:
            if not self._awaiting_id:
                return False
            self._human_text = text or ""
        self._human_event.set()
        return True

    def set_mic(self, on: bool) -> None:
        self.mic_on = bool(on)
        self.broker.publish({"type": "mic", "on": self.mic_on})

    def pause(self) -> None:
        """暂停：当前发言结束后，在进入下一发言位前停住。"""
        if self.state == "running":
            self._resume.clear()
            self.broker.publish({"type": "status", "state": "pausing"})

    def resume(self) -> None:
        """继续：解除暂停闸。"""
        self._resume.set()

    def stop(self) -> None:
        self._stop.set()
        self._resume.set()          # 解除暂停闸，让循环走到 stop 检查
        self._human_event.set()     # 解除可能的真人等待
        self._advance_event.set()   # 解除可能的「下一句」等待
        self.broker.publish({"type": "status", "state": "stopping"})

    # ------------------------------------------------------------------
    # 主审（可选）
    # ------------------------------------------------------------------
    def _mod_needed(self) -> bool:
        return self.cfg.use_moderator and bool(self.cfg.moderator_model)

    def _setup_moderator(self) -> None:
        self._mod_runner = OpenCodeRunner(model=self.cfg.moderator_model, timeout=180)
        self._mod_session = generate_session_id("live-moderator")
        sysp = prompts.build_moderator_system_prompt(self.cfg)
        mod_folder = self.work_dir / "moderator"
        mod_folder.mkdir(parents=True, exist_ok=True)
        methodology.install_into(mod_folder)
        self._mod_runner.run_safe(
            message=(
                f"[系统设定]\n{sysp}\n\n"
                "请先完整读取两份共享方法论与 `methodology/如何深化辩论.md`，"
                "确认将《如何深化辩论》作为每次点评的首要执行规范，再回复「主审已就位」。"
            ),
            session_id=self._mod_session, title="主审",
            model=self.cfg.moderator_model, cwd=mod_folder, pure=True,
            variant=self._variant(self.cfg.moderator_thinking))

    def _moderator_review(self, d: DebaterCfg, ph: PhaseSpec, text: str) -> None:
        self._moderator_review_phase(ph.label, [(d, ph, text)])

    def _moderator_review_phase(
        self,
        label: str,
        speeches: list[tuple[DebaterCfg, PhaseSpec, str]],
    ) -> None:
        """在完整环节结束后，汇总该环节全部发言并点评一次。"""
        if not self._mod_runner:
            return
        body = "\n\n".join(
            f"{d.name}（{d.side}）[{ph.label}]：\n{text}"
            for d, ph, text in speeches
            if text.strip()
        )
        if not body:
            return
        try:
            out = self._mod_runner.run_safe(
                message=(f"完整环节「{label}」已经结束，全部发言如下：\n----\n{body}\n----\n\n"
                         "请严格使用《如何深化辩论》的主审流程，对整个环节统一点评一次；"
                         "按“进展 / 分歧 / 下一问”组织，≤100 字，直指一个关键支点并逼向更深一层："),
                session_id=self._mod_session, title="主审点评",
                model=self.cfg.moderator_model, cwd=self.work_dir / "moderator",
                pure=True, variant=self._variant(self.cfg.moderator_thinking))
            out = (out or "").strip()
            if out and not out.startswith("[调用错误") and not out.lstrip().startswith("{"):
                out = truncate_at_sentence(out, 150)
                self._record("主审", "系统", f"{label} · 主审点评", out, "moderator")
                self.broker.publish({"type": "moderator", "label": label, "text": out})
        except Exception as e:
            logger.warning("主审点评失败: %s", e)

    # ------------------------------------------------------------------
    # 上下文 / 记录 / 工具
    # ------------------------------------------------------------------
    def _consume_new(self, debater_name: str) -> str:
        """惰性增量上下文：返回该 AI 辩手上次发言以来、他人新增的发言。"""
        seen = self._cursor.get(debater_name, 0)
        new = self.transcript[seen:]
        self._cursor[debater_name] = len(self.transcript)
        lines: list[str] = []
        for e in new:
            if e["speaker"] == debater_name:
                continue
            tag = "主审" if e["kind"] == "moderator" else f"{e['speaker']}（{e['side']}）"
            lines.append(f"[{e['label']}] {tag}：{e['text'][:300]}")
        return "\n".join(lines)

    def _last_question(self) -> str:
        return self._last_q

    def _last_opponent_text(self, my_side: str) -> str:
        for e in reversed(self.transcript):
            if e["kind"] in ("ai", "human") and e["side"] != my_side:
                return f"{e['speaker']}：{e['text']}"
        return ""

    def _record(self, speaker: str, side: str, label: str, text: str, kind: str) -> None:
        self.transcript.append({"speaker": speaker, "side": side, "label": label,
                                "text": text, "kind": kind, "char_count": len(text)})

    def _host(self, text: str) -> None:
        """主持人串场：仅文本，不合成语音（需求 5）。"""
        self.broker.publish({"type": "host", "text": text})

    @staticmethod
    def _variant(thinking: str) -> str:
        # "none" → 不传 --variant（部分模型不支持变体）。
        return "" if thinking == "none" else thinking
