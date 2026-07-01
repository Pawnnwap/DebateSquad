"""实时辩论的 stdlib HTTP + SSE 服务（无新依赖）。

  - GET  /                 → 单页 UI
  - GET  /api/options      → 模型/思考/音色等下拉项
  - POST /api/create       → 用 JSON 配置建立一场辩论
  - POST /api/prepare      → 触发 AI 备赛（后台）
  - POST /api/start        → 开始实时比赛（后台）
  - POST /api/human_turn   → 提交真人发言（解除阻塞）
  - POST /api/mic          → 真人麦克风开关状态
  - POST /api/stt_keywords → 用 opencode/big-pickle 提取 STT 辅助关键词
  - POST /api/stop         → 停止本场
  - GET  /api/state        → 当前快照
  - GET  /events           → SSE 事件流（server→browser 实时推送）
  - GET  /audio/<name>     → AI 发言音频 mp3

单进程单场：一台服务器同时只跑一场辩论（本地自用足够）。
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import engine, library, models, paths, stt, tts
from .engine import DebaterCfg, EventBroker, LiveConfig, LiveDebate

logger = logging.getLogger(__name__)


class LiveState:
    """进程级单场状态：一个 broker 贯穿建场前后，UI 刷新也能续看历史。"""
    def __init__(self) -> None:
        self.debate: LiveDebate | None = None
        self.broker = EventBroker()
        self.lock = threading.Lock()

    def create(self, cfg: LiveConfig, entry_id: str | None = None,
               mark_loaded: bool = False) -> LiveDebate:
        with self.lock:
            self.broker = EventBroker()          # 新一场 → 全新事件历史
            entry_id = entry_id or library.new_entry_id(cfg.topic)
            work = paths.runs_dir() / entry_id
            self.debate = LiveDebate(cfg, work, broker=self.broker, entry_id=entry_id)
            self.debate.save_meta()              # 立即落库，库列表里即可见
            if mark_loaded:
                self.debate.mark_loaded()        # 从库加载：直接备赛就绪，可跳过备赛开始
            return self.debate


STATE = LiveState()


class LiveHTTPServer(ThreadingHTTPServer):
    """Suppress normal browser/client disconnect noise while keeping real errors."""

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def _cfg_from_json(data: dict) -> LiveConfig:
    debaters = []
    for x in data.get("debaters", []):
        debaters.append(DebaterCfg(
            name=str(x.get("name", "")).strip() or "辩手",
            side=str(x.get("side", "")).strip() or "正方",
            kind="human" if x.get("kind") == "human" else "ai",
            stance=str(x.get("stance", "")).strip(),
            model=str(x.get("model", "") or "opencode/mimo-v2.5-free"),
            thinking=str(x.get("thinking", "medium") or "medium"),
            voice=str(x.get("voice", "") or tts.default_voice()),
            persona=str(x.get("persona", "")).strip(),
            custom_prompt=str(x.get("custom_prompt", "")).strip(),
            seat=int(x.get("seat", 0) or 0),
        ))
    if not debaters:
        raise ValueError("至少需要一名辩手")
    modules = _sanitize_modules(data.get("modules", []))

    def _num(key, default, cast, lo=None):
        # 不能用 `x or default`：那会把合法的 0（如 free_rounds=0 跳过自由辩论）误判为缺省。
        v = data.get(key, None)
        if v is None or v == "":
            return default
        try:
            v = cast(v)
        except (TypeError, ValueError):
            return default
        if lo is not None and v < lo:
            return default
        return v

    return LiveConfig(
        topic=str(data.get("topic", "")).strip() or "未命名辩题",
        debaters=debaters,
        rules=str(data.get("rules", "")).strip(),
        wpm=_num("wpm", 240, int, lo=20),
        free_rounds=_num("free_rounds", 3, int, lo=0),
        opening_minutes=_num("opening_minutes", engine.DEFAULT_OPENING_MIN, float, lo=0.1),
        free_minutes=_num("free_minutes", engine.DEFAULT_FREE_MIN, float, lo=0.1),
        closing_minutes=_num("closing_minutes", engine.DEFAULT_CLOSING_MIN, float, lo=0.1),
        use_moderator=bool(data.get("use_moderator", False)),
        moderator_model=str(data.get("moderator_model", "") or "opencode/mimo-v2.5-free"),
        moderator_thinking=str(data.get("moderator_thinking", "medium") or "medium"),
        double_check=bool(data.get("double_check", False)),
        manual_advance=bool(data.get("manual_advance", True)),
        tts_engine=tts.resolve_id(str(data.get("tts_engine", "") or "")),
        stt_keywords=_sanitize_keywords(data.get("stt_keywords", [])),
        stt_provider=stt.resolve_id(str(data.get("stt_provider", "") or "")),
        modules=modules,
    )


def _sanitize_keywords(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        word = str(item or "").strip()
        if not word or len(word) > 24 or word in seen:
            continue
        seen.add(word)
        out.append(word)
        if len(out) >= 80:
            break
    return out


def _sanitize_modules(raw) -> list:
    """校验/规整赛制模块列表（防御非法输入）。"""
    if not isinstance(raw, list):
        return []
    out = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        typ = m.get("type")
        if typ not in ("statement", "cross_exam", "free_debate"):
            continue
        try:
            minutes = float(m.get("minutes", 2) or 2)
        except (TypeError, ValueError):
            minutes = 2.0
        item = {"type": typ, "minutes": max(0.1, minutes), "label": str(m.get("label", "")).strip()}
        if typ == "free_debate":
            # participants：[[阵营,座位],...]，选定参加自由辩论的辩手；缺省/空 → 全体。
            refs = []
            for pair in m.get("participants", []) or []:
                try:
                    refs.append([str(pair[0]).strip(), int(pair[1])])
                except (TypeError, ValueError, IndexError):
                    continue
            if refs:
                item["participants"] = refs
        if typ == "statement":
            item["side"] = str(m.get("side", "")).strip() or "正方"
            item["seat"] = int(m.get("seat", 1) or 1)
            item["hint"] = str(m.get("hint", "")).strip()
            fn = str(m.get("function", "opening"))
            item["function"] = fn if fn in ("opening", "continuation", "cross_summary", "closing") else "opening"
        elif typ == "cross_exam":
            seen_refs: set[tuple[str, int]] = set()

            def _refs(key, seen=seen_refs):
                # 同一环节内同一辩手只取一次（质询/被质询合并去重）。
                r = []
                for pair in m.get(key, []) or []:
                    try:
                        ref = (str(pair[0]).strip(), int(pair[1]))
                    except (TypeError, ValueError, IndexError):
                        continue
                    if ref in seen:
                        continue
                    seen.add(ref)
                    r.append([ref[0], ref[1]])
                return r
            item["questioners"] = _refs("questioners")
            item["respondents"] = _refs("respondents")
            item["single_side"] = bool(m.get("single_side", True))
        out.append(item)
    return out


def _cfg_from_meta(meta: dict, briefs: dict) -> LiveConfig:
    """从库里的 meta.json + 已存简报重建配置：保留原 debater id，注入 brief（可跳过备赛）。"""
    debaters = []
    for x in meta.get("debaters", []):
        did = str(x.get("id") or "")
        kw = dict(
            name=str(x.get("name", "")).strip() or "辩手",
            side=str(x.get("side", "")).strip() or "正方",
            kind="human" if x.get("kind") == "human" else "ai",
            stance=str(x.get("stance", "")).strip(),
            model=str(x.get("model", "") or "opencode/mimo-v2.5-free"),
            thinking=str(x.get("thinking", "medium") or "medium"),
            voice=str(x.get("voice", "") or tts.default_voice()),
            persona=str(x.get("persona", "")).strip(),
            custom_prompt=str(x.get("custom_prompt", "")).strip(),
            brief=briefs.get(did, ""),
            seat=int(x.get("seat", 0) or 0),
        )
        d = DebaterCfg(**kw)
        if did:
            d.id = did
        debaters.append(d)
    if not debaters:
        raise ValueError("库条目缺少辩手")
    return LiveConfig(
        topic=str(meta.get("topic", "")).strip() or "未命名辩题",
        debaters=debaters,
        rules=str(meta.get("rules", "")).strip(),
        wpm=int(meta.get("wpm", 240) or 240),
        free_rounds=int(meta.get("free_rounds", 3)),
        opening_minutes=float(meta.get("opening_minutes", engine.DEFAULT_OPENING_MIN)),
        free_minutes=float(meta.get("free_minutes", engine.DEFAULT_FREE_MIN)),
        closing_minutes=float(meta.get("closing_minutes", engine.DEFAULT_CLOSING_MIN)),
        use_moderator=bool(meta.get("use_moderator", False)),
        moderator_model=str(meta.get("moderator_model", "") or "opencode/mimo-v2.5-free"),
        moderator_thinking=str(meta.get("moderator_thinking", "medium") or "medium"),
        double_check=bool(meta.get("double_check", False)),
        manual_advance=bool(meta.get("manual_advance", True)),
        tts_engine=tts.resolve_id(str(meta.get("tts_engine", "") or "")),
        stt_keywords=_sanitize_keywords(meta.get("stt_keywords", [])),
        stt_provider=stt.resolve_id(str(meta.get("stt_provider", "") or "")),
        modules=_sanitize_modules(meta.get("modules", [])),
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # 静音默认访问日志
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers --
    def _send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # -- routing --
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/" or path == "/index.html":
                self._serve_file(paths.static_dir() / "index.html", "text/html; charset=utf-8")
            elif path == "/api/library":
                self._send_json({"entries": library.list_entries()})
            elif path == "/api/transcript":
                from urllib.parse import parse_qs
                qid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
                self._send_json({"id": qid, "rows": library.load_transcript(qid),
                                 "meta": library.load_meta(qid)})
            elif path == "/api/export":
                from urllib.parse import parse_qs
                qid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
                d = STATE.debate
                # id 指定 → 导出库中该条；否则导出当前进行中的本场。
                if qid:
                    meta, rows = library.load_meta(qid), library.load_transcript(qid)
                elif d:
                    meta, rows = d.to_meta(), d.transcript
                else:
                    meta, rows = None, []
                md = library.to_markdown(meta, rows).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="debate.md"')
                self.send_header("Content-Length", str(len(md)))
                self.end_headers()
                self.wfile.write(md)
            elif path == "/api/options" or path == "/api/models":
                from urllib.parse import parse_qs
                refresh = parse_qs(urlparse(self.path).query).get("refresh", ["0"])[0] in ("1", "true")
                self._send_json({
                    # 每个模型自带 thinking 列表（其支持的思考强度），由 opencode 实时获取。
                    "models": models.fetch_models(refresh=refresh),
                    "thinking": engine.THINKING_CHOICES,   # 未知（手动输入）模型的通用回退
                    "voices": tts.all_voices(),
                    "tts_engines": tts.options(),          # 插件化 TTS 引擎下拉
                    "stt_providers": stt.options(),        # 插件化 STT 提供方下拉
                    "defaults": {"wpm": 240, "free_rounds": 3,
                                 "opening_minutes": engine.DEFAULT_OPENING_MIN,
                                 "free_minutes": engine.DEFAULT_FREE_MIN,
                                 "closing_minutes": engine.DEFAULT_CLOSING_MIN},
                })
            elif path == "/api/state":
                d = STATE.debate
                self._send_json(d.snapshot() if d else {"state": "idle"})
            elif path == "/api/briefs":
                d = STATE.debate
                self._send_json({"briefs": d.briefs() if d else []})
            elif path == "/events":
                self._serve_sse()
            elif path.startswith("/audio/"):
                self._serve_audio(path[len("/audio/"):])
            else:
                self._send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            logger.exception("GET %s 失败", path)
            try:
                self._send_json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            data = self._read_json()
            if path == "/api/create":
                cfg = _cfg_from_json(data)
                STATE.create(cfg)
                self._send_json({"ok": True, "snapshot": STATE.debate.snapshot()})
            elif path == "/api/load":
                eid = str(data.get("id", ""))
                meta = library.load_meta(eid)
                if not meta:
                    raise ValueError("找不到该保存的辩论")
                cfg = _cfg_from_meta(meta, library.load_briefs(eid))
                has_prep = any(d.brief for d in cfg.debaters)
                STATE.create(cfg, entry_id=eid, mark_loaded=has_prep)
                self._send_json({"ok": True, "has_prep": has_prep, "meta": meta,
                                 "snapshot": STATE.debate.snapshot()})
            elif path == "/api/library_delete":
                ok = library.delete_entry(str(data.get("id", "")))
                self._send_json({"ok": ok})
            elif path == "/api/prepare":
                self._need_debate().prepare()
                self._send_json({"ok": True})
            elif path == "/api/start":
                self._need_debate().start()
                self._send_json({"ok": True})
            elif path == "/api/human_turn":
                ok = self._need_debate().submit_human(str(data.get("text", "")))
                self._send_json({"ok": ok})
            elif path == "/api/mic":
                self._need_debate().set_mic(bool(data.get("on", True)))
                self._send_json({"ok": True})
            elif path == "/api/stt_keywords":
                stances = []
                for d in data.get("debaters", []) or []:
                    if isinstance(d, dict):
                        stance = str(d.get("stance", "")).strip()
                        if stance and stance not in stances:
                            stances.append(stance)
                res = stt.extract(
                    stt.resolve_id(str(data.get("stt_provider", "") or "")),
                    topic=str(data.get("topic", "")).strip(),
                    rules=str(data.get("rules", "")).strip(),
                    stances=stances,
                )
                self._send_json({"ok": True, **res})
            elif path == "/api/advance":
                ok = self._need_debate().advance()
                self._send_json({"ok": ok})
            elif path == "/api/pause":
                self._need_debate().pause()
                self._send_json({"ok": True})
            elif path == "/api/resume":
                self._need_debate().resume()
                self._send_json({"ok": True})
            elif path == "/api/stop":
                self._need_debate().stop()
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "not found"}, 404)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            logger.exception("POST %s 失败", path)
            self._send_json({"error": str(e)}, 500)

    def _need_debate(self) -> LiveDebate:
        if STATE.debate is None:
            raise ValueError("尚未建立辩论，请先创建")
        return STATE.debate

    # -- static / audio --
    def _serve_file(self, fp: Path, ctype: str) -> None:
        if not fp.exists():
            self._send_json({"error": "missing"}, 404)
            return
        self._send_bytes(fp.read_bytes(), ctype)

    def _serve_audio(self, name: str) -> None:
        d = STATE.debate
        if d is None:
            self._send_json({"error": "no debate"}, 404)
            return
        # 防目录穿越：只取文件名部分
        fp = d.audio_dir / Path(name).name
        if not fp.exists():
            self._send_json({"error": "missing"}, 404)
            return
        # 按扩展名给正确 MIME（支持 mp3/wav/ogg 等插件引擎产出的格式）。
        ctype = {".mp3": "audio/mpeg", ".wav": "audio/wav",
                 ".ogg": "audio/ogg", ".m4a": "audio/mp4"}.get(
                     fp.suffix.lower(), "application/octet-stream")
        self._send_bytes(fp.read_bytes(), ctype)

    # -- SSE --
    def _serve_sse(self) -> None:
        # 断线重连：浏览器带 Last-Event-ID（上次收到的 seq）→ 只补发更新的事件，避免整段重放导致重复渲染。
        try:
            since = int(self.headers.get("Last-Event-ID", "-1"))
        except (TypeError, ValueError):
            since = -1
        q = STATE.broker.subscribe(since_seq=since)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                    payload = (f"id: {ev['seq']}\n"
                               "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n")
                    self.wfile.write(payload.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")   # 心跳，维持连接
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            STATE.broker.unsubscribe(q)


def serve(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = False) -> None:
    paths.runs_dir(); paths.library_dir()        # 确保数据目录就绪
    # port=0 或被占用 → 让 OS 选空闲端口，避免「端口被占」启动失败（打包后尤其稳）。
    httpd = LiveHTTPServer((host, port), Handler)
    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}"
    logger.info("实时辩论服务已启动: %s", url)
    logger.info("数据目录（保存的辩论/备赛/记录）: %s", paths.data_home())
    print(f"\n  ▶ 实时辩论已就绪，请在浏览器打开： {url}")
    print(f"  ▶ 保存目录： {paths.data_home()}\n")
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
