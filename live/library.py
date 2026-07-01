"""辩论库：保存 / 列出 / 读取 过往辩论的配置、备赛简报与发言记录。

目录结构（每条一个文件夹，位于 paths.library_dir() 下）：
    <entry_id>/
        meta.json            # 完整 LiveConfig（含每位辩手 id/模型/阵营等）+ 创建时间
        briefs/<debater_id>.md   # 各 AI 辩手的备赛简报（用于「跳过备赛直接辩论」）
        transcript.jsonl     # 完成后的逐条发言记录（每行一个 JSON）

「跳过备赛直接辩论」即：从库里 load 一条目 → 各 AI 辩手带着已保存的简报直接上场，
无需重新联网备赛。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import paths

logger = logging.getLogger(__name__)


def _slug(text: str, maxlen: int = 24) -> str:
    s = re.sub(r"[^\w一-鿿]+", "-", (text or "").strip()).strip("-")
    return s[:maxlen] or "debate"


def new_entry_id(topic: str) -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + _slug(topic)


def entry_dir(entry_id: str) -> Path:
    # 防穿越：只取末段名字
    return paths.library_dir() / Path(entry_id).name


def save_meta(entry_id: str, meta: dict) -> None:
    d = entry_dir(entry_id)
    d.mkdir(parents=True, exist_ok=True)
    meta = {**meta, "id": entry_id, "saved_at": datetime.now().isoformat()}
    if "created_at" not in meta:
        meta["created_at"] = meta["saved_at"]
    (d / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def save_brief(entry_id: str, debater_id: str, text: str) -> None:
    bd = entry_dir(entry_id) / "briefs"
    bd.mkdir(parents=True, exist_ok=True)
    (bd / (Path(debater_id).name + ".md")).write_text(text or "", encoding="utf-8")


def load_briefs(entry_id: str) -> dict[str, str]:
    bd = entry_dir(entry_id) / "briefs"
    out: dict[str, str] = {}
    if bd.is_dir():
        for f in bd.glob("*.md"):
            try:
                out[f.stem] = f.read_text(encoding="utf-8")
            except OSError:
                pass
    return out


def save_transcript(entry_id: str, transcript: list[dict]) -> None:
    d = entry_dir(entry_id)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "transcript.jsonl", "w", encoding="utf-8") as f:
        for row in transcript:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_meta(entry_id: str) -> Optional[dict]:
    f = entry_dir(entry_id) / "meta.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("读取库条目失败 %s: %s", entry_id, e)
        return None


def load_transcript(entry_id: str) -> list[dict]:
    f = entry_dir(entry_id) / "transcript.jsonl"
    rows: list[dict] = []
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def list_entries() -> list[dict]:
    """按时间倒序列出库内全部条目（含是否已备赛 / 是否有记录）。"""
    out: list[dict] = []
    for d in paths.library_dir().iterdir():
        if not d.is_dir():
            continue
        meta = load_meta(d.name)
        if not meta:
            continue
        briefs = (d / "briefs")
        has_prep = briefs.is_dir() and any(briefs.glob("*.md"))
        out.append({
            "id": meta.get("id", d.name),
            "topic": meta.get("topic", "(无题)"),
            "created_at": meta.get("created_at", ""),
            "saved_at": meta.get("saved_at", ""),
            "debaters": [{"name": x.get("name"), "side": x.get("side"),
                          "kind": x.get("kind")} for x in meta.get("debaters", [])],
            "has_prep": bool(has_prep),
            "has_transcript": (d / "transcript.jsonl").exists(),
        })
    out.sort(key=lambda e: e.get("saved_at", ""), reverse=True)
    return out


def to_markdown(meta: Optional[dict], rows: list[dict]) -> str:
    """把一场辩论（meta + 逐条发言）渲染成可读 Markdown，用于导出/下载。"""
    meta = meta or {}
    lines: list[str] = []
    lines.append(f"# 辩论记录：{meta.get('topic', '(无题)')}\n")
    if meta.get("created_at"):
        lines.append(f"- 时间：{meta['created_at']}")
    ds = meta.get("debaters") or []
    if ds:
        who = "，".join(f"{d.get('name')}（{d.get('side')}·{'真人' if d.get('kind')=='human' else 'AI'}）" for d in ds)
        lines.append(f"- 参辩：{who}")
    if meta.get("rules"):
        lines.append(f"- 规则：{meta['rules']}")
    lines.append("\n---\n")
    for r in rows:
        kind = r.get("kind")
        label = r.get("label", "")
        speaker = r.get("speaker", "")
        text = (r.get("text") or "").strip()
        if not text:
            continue
        if kind == "moderator":
            lines.append(f"> **⚖️ 主审（{label}）**：{text}\n")
        elif kind in ("ai", "human"):
            badge = "🧑" if kind == "human" else "🤖"
            lines.append(f"**{badge} {speaker}**（{r.get('side','')} · {label}）\n\n{text}\n")
        else:
            lines.append(f"_{text}_\n")
    return "\n".join(lines)


def delete_entry(entry_id: str) -> bool:
    import shutil
    d = entry_dir(entry_id)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False
