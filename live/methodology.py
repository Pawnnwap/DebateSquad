"""Bundle and provision shared debate methodology for every AI role."""

from __future__ import annotations

from functools import lru_cache
import shutil
import sys
from pathlib import Path

SHARED_METHODOLOGY_FILES: tuple[str, ...] = (
    "辩论方法论.md",
    "逻辑与论辩学理论.md",
)
MODERATOR_METHODOLOGY_FILE = "如何深化辩论.md"
METHODOLOGY_FILES: tuple[str, ...] = (
    *SHARED_METHODOLOGY_FILES,
    MODERATOR_METHODOLOGY_FILE,
)


def source_dir() -> Path:
    """Return bundled/source methodology directory."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidate = base / "methodology"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent.parent / "methodology"


@lru_cache(maxsize=1)
def validate_sources() -> tuple[Path, ...]:
    """Fail early when a required methodology document is absent or empty."""
    paths: list[Path] = []
    for name in METHODOLOGY_FILES:
        path = source_dir() / name
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"缺少辩论方法论资源：{path}")
        paths.append(path)
    return tuple(paths)


def install_into(work_dir: str | Path) -> Path:
    """Copy methodology into one isolated side/moderator workspace."""
    target = Path(work_dir) / "methodology"
    target.mkdir(parents=True, exist_ok=True)
    for source in validate_sources():
        shutil.copy2(source, target / source.name)
    return target


def prompt_instructions(role: str) -> str:
    """Short mandatory read/use contract; full documents stay on disk."""
    if role == "moderator":
        files = "、".join(f"`methodology/{name}`" for name in METHODOLOGY_FILES)
        use = (
            "必须把《如何深化辩论》作为主审点评的首要执行规范，逐次落实“对齐、检验、深化、"
            "找支点、综合”，并用“进展 / 分歧 / 下一问”组织输出；再用另外两份文件中的"
            "图尔明模型、证据五维度、谬误检查和论证负担补强判断。不得用泛泛的逻辑点评"
            "替代《如何深化辩论》的流程。"
        )
    elif role == "prep":
        files = "、".join(f"`methodology/{name}`" for name in SHARED_METHODOLOGY_FILES)
        use = (
            "把其中的破题、资料分级、论点构建、证据核验、攻防预演方法落实到备赛文件；"
            "不能只复述目录或声称已读。"
        )
    else:
        files = "、".join(f"`methodology/{name}`" for name in SHARED_METHODOLOGY_FILES)
        use = (
            "把其中的论证结构、证据评估、质询、驳论、自由辩论与总结方法落实到实际发言；"
            "不能只复述目录或声称已读。"
        )
    return (
        "## 共享方法论（强制）\n"
        f"开始工作前完整读取 {files}。{use}\n"
        "这些文件已复制到你的工作目录，可用相对路径读取。\n"
    )
