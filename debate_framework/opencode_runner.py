"""OpenCode CLI 统一运行器模块。

项目中所有 opencode CLI 调用的唯一入口。封装 subprocess 调用，
提供会话管理、智能重试和统一 JSON 解析。

用法:
    runner = OpenCodeRunner(model="opencode/mimo-v2.5-free")
    session_id = runner.create_session("标题", "系统提示词...")
    response = runner.run("消息", session_id, cwd="/path/to/dir", pure=True)
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import random
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 共享常量
# ---------------------------------------------------------------------------

_KNOWN_OPENCODE_PATHS = [
    r"C:\Users\Vladi\AppData\Roaming\npm\node_modules\opencode-ai\bin\opencode.exe",
    "/usr/local/bin/opencode",
    "/opt/homebrew/bin/opencode",
]


def _find_opencode() -> str:
    """查找 opencode 二进制，优先级：环境变量 > 已知路径 > PATH。"""
    if env_path := os.environ.get("OPENCODE_CLI"):
        return env_path
    for p in _KNOWN_OPENCODE_PATHS:
        if Path(p).exists():
            return p
    return "opencode"

# opencode 可执行文件路径（可用环境变量 OPENCODE_CLI 覆盖）
OPENCODE_BIN: str = _find_opencode()

_DEFAULT_MODEL: str = "claude-sonnet-4-5-20250929"
_DEFAULT_TIMEOUT: int = 150          # 单次调用超时（秒）：超时即判失败重试，缩短"长等待"
_MAX_RETRIES: int = 6                 # 多重试几次，骑过 provider 间歇性掉线
_RETRY_BACKOFF_BASE: float = 2.0
# "供应商不可用"类错误（No provider available / 401 / ModelError）：provider 临时掉线，
# 需更长退避等其恢复，而非 2-4 秒立刻重撞同一死 provider。
_PROVIDER_DOWN_MARKERS = ("No provider available", "ModelError", "isRetryable", "401")
_FALLBACK_TEXT_LENGTH: int = 3000

# 中文句子结束正则（供外部复用）
SENTENCE_END_PATTERN = re.compile(r"[。！？；…]|[\.\!\?\;\n](?!\w)")
FALLBACK_BOUNDARY_PATTERN = re.compile(r"[，,、\s]")

# opencode 服务端 session id（ses_xxx），用于「先创建后续接」会话管理
SESSION_ID_PATTERN = re.compile(r'"sessionID"\s*:\s*"(ses_[^"]+)"')

# ---------------------------------------------------------------------------
# 异常类
# ---------------------------------------------------------------------------

class OpenCodeError(Exception):
    """OpenCode CLI 调用错误。"""
    pass


class OpenCodeTimeoutError(OpenCodeError):
    """OpenCode CLI 调用超时。"""
    pass


class OpenCodeParseError(OpenCodeError):
    """OpenCode 响应解析错误。"""
    pass


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def generate_session_id(prefix: str = "debate") -> str:
    """生成唯一的 session ID。

    Args:
        prefix: ID 前缀。

    Returns:
        "{prefix}-{uuid_hex_8}" 格式的 ID。
    """
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def generate_debater_session_id(team: str, position: str) -> str:
    """生成辩手 session ID。

    Args:
        team: "正方" 或 "反方"。
        position: "一" / "二" / "三" / "四"。

    Returns:
        "debate-{team}-{position}-{uuid_hex_8}" 格式的 ID。
    """
    return generate_session_id(f"debate-{team}-{position}")


def truncate_at_sentence(text: str, max_chars: int) -> str:
    """在句子边界截断文本（供外部直接使用）。

    优先在 。！？；… 处截断，回退到逗号/空格。

    Args:
        text: 原始文本。
        max_chars: 最大字符数。

    Returns:
        截断后的文本。
    """
    if len(text) <= max_chars:
        return text

    search = text[:max_chars]
    matches = list(SENTENCE_END_PATTERN.finditer(search))
    if matches:
        return text[:matches[-1].end()]

    fb_matches = list(FALLBACK_BOUNDARY_PATTERN.finditer(search))
    if fb_matches:
        return text[:fb_matches[-1].end()]

    return text[:max_chars]


# ---------------------------------------------------------------------------
# 统一 OpenCode 运行器
# ---------------------------------------------------------------------------

class OpenCodeRunner:
    """OpenCode CLI 统一运行器。

    项目中所有 opencode 调用的唯一入口，支持路径隔离、纯文本模式、
    自动重试和统一 JSON 解析。

    Attributes:
        path: opencode CLI 可执行文件路径。
        model: 默认模型名称。
        timeout: 默认超时时间（秒）。
    """

    def __init__(
        self,
        opencode_path: str = OPENCODE_BIN,
        model: str = _DEFAULT_MODEL,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        self.path = opencode_path
        self.model = model
        self.timeout = timeout
        # 客户端 session_id -> opencode 服务端 ses_xxx 映射。
        # opencode 1.16+ 不再接受客户端自选 session id，必须「先创建后续接」：
        # 首次调用不传 --session（创建），从输出捕获真实 ses_xxx，后续用其续接。
        self._session_map: dict[str, str] = {}
        self._session_lock = threading.Lock()
        # opencode 使用全局 sqlite 会话库，--dir 不隔离它。仅非 pure 调用（含快照等
        # 重写入）并发时会触发 "database is locked"，故只对非 pure 调用经此锁串行化；
        # pure 调用（备赛/辩手初始化/比赛发言）可安全并发（详见 run()）。
        self._run_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    def run(
        self,
        message: str,
        session_id: str,
        title: str = "",
        model: str = "",
        timeout: Optional[int] = None,
        cwd: Optional[Path] = None,
        pure: bool = False,
        variant: str = "",
    ) -> str:
        """调用 opencode run 发送消息并返回 AI 响应。

        会话管理采用「先创建后续接」：客户端 session_id 首次出现时不传
        --session（由 opencode 创建并返回 ses_xxx），捕获后存入映射；
        后续同一 session_id 用 --session ses_xxx 续接，保持上下文连续。

        Args:
            message: 发送给 AI 的消息内容。
            session_id: 客户端会话 ID（逻辑标识，非 opencode 真实 ses id）。
            title: 会话标题。
            model: 模型名称（默认使用实例 model）。
            timeout: 超时秒数（默认使用实例 timeout）。
            cwd: 工作目录，设置后 opencode --dir 指向该路径。
            pure: 是否使用 --pure 模式（禁用外部插件）。
            variant: 模型变体 / 推理强度（high/medium/low），映射到 --variant。

        Returns:
            AI 响应文本。

        Raises:
            OpenCodeTimeoutError: 调用超时。
            OpenCodeError: 其他调用错误。
        """
        model = model or self.model
        title = title or session_id or "opencode"
        timeout = timeout or self.timeout

        with self._session_lock:
            real_ses = self._session_map.get(session_id)

        cmd = [
            self.path, "run",
            message,
            "--title", title,
            "-m", model,
            "--format", "json",
        ]
        # 已有真实 ses → 续接；否则本次为创建（不传 --session）
        if real_ses:
            cmd.extend(["--session", real_ses])
        if cwd is not None:
            cmd.extend(["--dir", str(cwd)])
        if pure:
            cmd.append("--pure")
        if variant:
            cmd.extend(["--variant", variant])

        logger.debug("opencode: session=%s(ses=%s), model=%s, variant=%s, pure=%s, cwd=%s",
                      session_id, real_ses, model, variant, pure, cwd)

        # pure 调用无外部插件、写入轻量，可安全并发（实测 8 路并发无锁冲突）；
        # 非 pure 调用涉及快照等重写入，并发会触发 sqlite "database is locked"，故串行化。
        run_lock = contextlib.nullcontext() if pure else self._run_lock

        last_error: Optional[Exception] = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                with run_lock:
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        encoding="utf-8",
                        errors="replace",
                    )
                if result.returncode != 0:
                    # rc!=0 时错误常在 stdout 的 JSON 里（--format json），stderr 可能为空；
                    # 故回退到 stdout 末段，便于诊断真实失败原因。
                    err = (result.stderr.strip()
                           or result.stdout.strip()[-600:] or "未知错误")
                    logger.warning("opencode rc=%d (attempt %d/%d): %s",
                                   result.returncode, attempt, _MAX_RETRIES, err[:500])
                    if attempt < _MAX_RETRIES:
                        # provider 掉线 → 长退避（最多 60s）等其恢复；其余 → 指数退避（封顶 30s）。
                        # 均加抖动，避免并发调用退避后再次同步撞锁。
                        if any(k in err for k in _PROVIDER_DOWN_MARKERS):
                            time.sleep(min(60.0, 15.0 * attempt) + random.uniform(0, 3))
                        else:
                            time.sleep(min(30.0, _RETRY_BACKOFF_BASE ** attempt) + random.uniform(0, 1))
                        continue
                    raise OpenCodeError(f"opencode 退出码 {result.returncode}: {err}")

                # 首次创建会话：捕获 opencode 返回的真实 ses_xxx 供后续续接
                if real_ses is None:
                    m = SESSION_ID_PATTERN.search(result.stdout)
                    if m:
                        with self._session_lock:
                            self._session_map.setdefault(session_id, m.group(1))

                return self._parse_response(result.stdout)

            except subprocess.TimeoutExpired:
                logger.warning("opencode 超时 (attempt %d/%d)", attempt, _MAX_RETRIES)
                last_error = OpenCodeTimeoutError(f"opencode 调用超时 ({timeout}s)")
                if attempt < _MAX_RETRIES:
                    time.sleep(min(30.0, _RETRY_BACKOFF_BASE ** attempt) + random.uniform(0, 1))

            except (OpenCodeParseError, json.JSONDecodeError) as e:
                last_error = e
                if attempt < _MAX_RETRIES:
                    time.sleep(min(30.0, _RETRY_BACKOFF_BASE ** attempt) + random.uniform(0, 1))

        raise OpenCodeError(f"opencode 失败 (已重试 {_MAX_RETRIES} 次): {last_error}")

    def create_session(
        self,
        title: str,
        system_prompt: str,
        model: str = "",
        cwd: Optional[Path] = None,
        pure: bool = False,
    ) -> str:
        """创建新的 opencode 会话并发送系统提示词。

        Args:
            title: 会话标题。
            system_prompt: 系统提示词。
            model: 模型名称。
            cwd: 工作目录。
            pure: 是否使用纯文本模式。

        Returns:
            新 session ID。
        """
        session_id = generate_session_id()
        init_msg = (
            f"[系统设定]\n{system_prompt}\n\n"
            "请回复「已就绪」，确认你已理解自己的角色。"
        )
        try:
            self.run(
                message=init_msg, session_id=session_id,
                title=title, model=model or self.model,
                cwd=cwd, pure=pure,
            )
            logger.info("会话已创建: %s", session_id)
        except OpenCodeError as e:
            logger.warning("会话初始化发送失败: %s", e)
        return session_id

    def run_safe(
        self,
        message: str, session_id: str, title: str = "",
        model: str = "", cwd: Optional[Path] = None, pure: bool = False,
        variant: str = "",
    ) -> str:
        """安全调用（不抛异常，返回错误字符串）。

        Args:
            (同 run)

        Returns:
            AI 响应文本或错误描述。
        """
        try:
            return self.run(message, session_id, title, model,
                            cwd=cwd, pure=pure, variant=variant)
        except Exception as e:
            logger.error("opencode safe_run 失败: %s", e)
            return f"[调用错误: {e}]"

    def session_list(self) -> str:
        """列出所有 opencode 会话。"""
        cmd = [self.path, "session", "list"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                encoding="utf-8", errors="replace")
        return result.stdout

    def session_delete(self, session_id: str) -> None:
        """删除一个 opencode 会话。"""
        cmd = [self.path, "session", "delete", session_id]
        subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                       encoding="utf-8", errors="replace")

    # --------------------------------------------------------------
    # 资源清理
    # --------------------------------------------------------------

    def cleanup_sessions(self) -> int:
        """删除所有已创建的 opencode 会话，返回删除数量。

        遍历 _session_map 中的每个客户端 session_id，
        调用 session_delete() 删除对应的 opencode 服务端 ses_xxx。
        """
        with self._session_lock:
            sids = list(self._session_map.values())

        count = 0
        for sid in sids:
            try:
                self.session_delete(sid)
                count += 1
            except Exception as e:
                logger.debug("删除 session %s 失败: %s", sid, e)

        with self._session_lock:
            self._session_map.clear()

        if count:
            logger.info("已清理 %d 个 opencode 会话", count)
        return count

    @staticmethod
    def kill_lingering_processes() -> int:
        """杀死所有 opencode 残留进程，返回杀死数量。

        用于辩论结束后的彻底清理，确保没有 opencode agent 子进程残留。
        """
        import platform

        killed = 0
        try:
            system = platform.system()
            if system == "Windows":
                result = subprocess.run(
                    ["taskkill", "/F", "/IM", "opencode.exe"],
                    capture_output=True, text=True, timeout=15,
                )
                killed = result.stdout.count("SUCCESS")
            else:
                result = subprocess.run(
                    ["pkill", "-9", "-f", "opencode"],
                    capture_output=True, text=True, timeout=15,
                )
                killed = 1 if result.returncode == 0 else 0
        except Exception as e:
            logger.debug("清理 opencode 进程失败: %s", e)

        if killed:
            logger.info("已清理 %d 个 opencode 残留进程", killed)
        return killed

    # ------------------------------------------------------------------
    # JSON 解析（统一处理所有已知格式）
    # ------------------------------------------------------------------

    def _parse_response(self, stdout: str) -> str:
        """解析 opencode --format json 输出，统一处理所有已知格式。

        支持的格式:
        1. JSON Lines: type=text → part.text
        2. JSON Lines: type=assistant → content[*].text
        3. JSON Lines: 通用键扫描
        4. 整体 JSON 对象
        5. 原始文本回退

        Raises:
            OpenCodeParseError: 输出为空。
        """
        if not stdout.strip():
            raise OpenCodeParseError("opencode 返回空输出")

        lines = [l.strip() for l in stdout.strip().split("\n") if l.strip()]
        texts: list[str] = []

        for line in lines:
            try:
                obj = json.loads(line)
                obj_type = obj.get("type", "")

                # 格式1: type=text, text in part.text
                if obj_type == "text":
                    part = obj.get("part", {})
                    if isinstance(part, dict) and part.get("text"):
                        texts.append(part["text"])
                    continue

                # 格式2: type=assistant, text in content blocks
                if obj_type == "assistant":
                    for block in obj.get("content", []):
                        if isinstance(block, dict) and block.get("text"):
                            texts.append(block["text"])
                    continue

                # 格式3: 通用扫描 (top-level + part)
                for source in (obj, obj.get("part", {})):
                    if not isinstance(source, dict):
                        continue
                    for key in ("text", "response", "content", "message", "output"):
                        val = source.get(key, "")
                        if isinstance(val, str) and val.strip():
                            texts.append(val.strip())
                            break

            except json.JSONDecodeError:
                continue

        if texts:
            return "\n".join(texts)

        # 尝试整体 JSON
        try:
            obj = json.loads(stdout.strip())
            for key in ("response", "content", "message", "text", "output"):
                if key in obj and isinstance(obj[key], str) and obj[key].strip():
                    return obj[key].strip()
        except json.JSONDecodeError:
            pass

        logger.warning("无法解析 opencode JSON，返回原始文本")
        return stdout.strip()[:_FALLBACK_TEXT_LENGTH]
