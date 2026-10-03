"""OpenAI のリアルタイムの音声セッションを計装し、既存の事象の形へ写す。

音声のセッションは、話しながら裏のモデルがツールを呼ぶ。チャットの計装はこのセッションを包まないため、
セッションに与えた指示とペルソナ、話者の発話、ツール呼び出し、裏のモデルへの引き継ぎが監査に
現れない。周囲の音や再生された音声に指示を紛れ込ませる注入は、音声に固有の攻撃面になる。

**新しい種別を作らず、既存の事象へ写す。** 写せば受け取り側の今の規則がそのまま効く。

- セッションの指示とペルソナ ``session.update`` → ``llm.request`` の指示の欄。指示のダイジェストを
  付け、指示ファイルの伝播の判定が読めるようにする
- 発話の文字起こし ``conversation.item.input_audio_transcription.completed`` → ``llm.request`` の
  入力の欄。検知の走査だけに使う文を ``input_scan`` に添え、注入の判定が読めるようにする
- ツール呼び出し ``response.function_call_arguments.done`` → ``tool_call.requested`` と、提示した
  ツールからの選択として ``agent.decision``。結果の ``function_call_output`` → ``tool_call.completed``
- 提供元が実行する MCP の呼び出し ``mcp_call`` → ``mcp.tool_call.completed``
- 引き継ぎ ``RealtimeHandoffEvent`` → ``agent.decision``

**生の音声は送らない。** 事象の全体を写さず、決めた項目だけを取り出す。音声を運ぶ事象
``input_audio_buffer.append`` と ``response.output_audio.delta`` は読まずに捨てる。利用者の発話の
項目が音声の本体を持っていても、文字起こしの項目だけを採る。

**文字起こしにも今の秘匿を通す。** 走査の文は戻り値の走査の文と同じ実装で作り、送出の前に事象
全体へ秘匿が掛かる。

**turn の識別子を付ける。** 確定した発話ごとに turn を始め、その発話に続く応答とツール呼び出しを同じ
turn へ置く。1 つのセッションの事象は 1 つの run に置く。

計装する場所は 2 つある。OpenAI の SDK の接続の送受信と、Agents SDK のリアルタイムの模型の送受信である。
Agents SDK は自前の接続を持ち、OpenAI の SDK の接続を通らない。どちらも入っていなければ何もしない。
"""

from __future__ import annotations

import base64
import contextlib
import json
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from typing import Any

from senda_argus_hooks.core.context import (
    get_run_id,
    new_run_id,
    new_turn_id,
    reset_run_id,
    reset_turn_id,
    set_run_id,
    set_turn_id,
)
from senda_argus_hooks.core.hashing import sha256_value
from senda_argus_hooks.core.instruction_files import (
    classify_instruction_write,
    system_prompt_line_digests,
    system_prompt_pair_digests,
    system_prompt_semantic_digests,
)
from senda_argus_hooks.core.local_transcription import Transcriber
from senda_argus_hooks.core.mcp_tools import offered_alternatives
from senda_argus_hooks.core.result_scan import input_scan_fields, result_scan_fields
from senda_argus_hooks.core.runtime import emit_event, get_config

from .base import BaseInstrumentor, audit_guard

FRAMEWORK = "openai_realtime"

# 1 つのセッションで覚えておく項目の上限。どれも提供元とアプリが決める値で、長いセッションでも
# 際限なく伸びないようにする。超えたら古いものから捨てる。
_MAX_CALLS = 512
_MAX_ITEMS = 512
_MAX_TOOLS = 256
# ローカルの文字起こしのために手元に溜める音声の上限。24kHz の 16 ビットで約 87 秒にあたる。
# 超えた分は溜めず、切り詰めたことを印で示す。
_MAX_AUDIO_BYTES = 8 * 1024 * 1024

# 受け取った事象のうち読むもの。音声を運ぶ事象は大きく、受信のたびに解析すると遅れになる。
# 解析の前に、読む種別の名前が本文に現れるかだけを見る。
_SERVER_TYPES = (
    "session.created",
    "session.updated",
    "input_audio_buffer.committed",
    "input_audio_buffer.cleared",
    "conversation.item.input_audio_transcription.completed",
    "response.function_call_arguments.done",
    "response.output_item.done",
)
_SERVER_MARKERS = tuple(t.encode() for t in _SERVER_TYPES)
_CLIENT_TYPES = (
    "session.update",
    "conversation.item.create",
    "input_audio_buffer.append",
    "input_audio_buffer.clear",
)


@dataclass(frozen=True)
class _Format:
    encoding: str
    rate: int


_DEFAULT_FORMAT = _Format("pcm16", 24_000)

# 利用者がローカルの文字起こしを差し込む場所。計装の生成時にも渡せる。
_local_transcriber: Transcriber | None = None


def set_local_transcriber(transcriber: Transcriber | None) -> None:
    """文字起こしが届かない構成で使う、ローカルの文字起こしを差し込む。None で外す。"""
    global _local_transcriber
    _local_transcriber = transcriber


def _event_type(event: Any) -> str:
    if isinstance(event, dict):
        value = event.get("type")
    else:
        value = getattr(event, "type", None)
    return value if isinstance(value, str) else ""


def _as_dict(event: Any) -> dict[str, Any] | None:
    if isinstance(event, dict):
        return event
    if isinstance(event, (bytes, bytearray, str)):
        try:
            value = json.loads(event)
        except (ValueError, RecursionError):
            return None
        return value if isinstance(value, dict) else None
    for attr in ("model_dump", "dict"):
        fn = getattr(event, attr, None)
        if fn is not None:
            with contextlib.suppress(Exception):
                value = fn()
                if isinstance(value, dict):
                    return value
    return None


def _get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _session_tool_names(session: dict[str, Any]) -> list[str] | None:
    tools = session.get("tools")
    if not isinstance(tools, list):
        return None
    names: list[str] = []
    for tool in tools[:_MAX_TOOLS]:
        name = _str(_get(tool, "name")) or _str(_get(_get(tool, "function"), "name"))
        if name:
            names.append(name)
    return names


def _transcription_setting(session: dict[str, Any]) -> bool | None:
    """セッションが提供元の文字起こしを有効にしているか。設定に現れなければ None を返す。"""
    audio = session.get("audio")
    if isinstance(audio, dict):
        inp = audio.get("input")
        if isinstance(inp, dict) and "transcription" in inp:
            return bool(inp.get("transcription"))
    if "input_audio_transcription" in session:
        return bool(session.get("input_audio_transcription"))
    return None


def _input_format(session: dict[str, Any]) -> _Format | None:
    audio = session.get("audio")
    fmt: Any = None
    if isinstance(audio, dict) and isinstance(audio.get("input"), dict):
        fmt = audio["input"].get("format")
    if fmt is None:
        fmt = session.get("input_audio_format")
    if isinstance(fmt, str):
        name = fmt
        rate = 8_000 if "g711" in fmt else 24_000
    elif isinstance(fmt, dict):
        name = _str(fmt.get("type"))
        rate = fmt.get("rate") if isinstance(fmt.get("rate"), int) else 24_000
    else:
        return None
    encoding = {
        "audio/pcm": "pcm16",
        "pcm16": "pcm16",
        "audio/pcmu": "g711_ulaw",
        "g711_ulaw": "g711_ulaw",
        "audio/pcma": "g711_alaw",
        "g711_alaw": "g711_alaw",
    }.get(name)
    if encoding is None:
        return None
    if encoding != "pcm16":
        rate = 8_000
    return _Format(encoding, rate)


def _user_texts(item: dict[str, Any]) -> list[str]:
    """利用者の発話の項目から、文字の部分だけを取り出す。音声の本体は取らない。"""
    texts: list[str] = []
    content = item.get("content")
    if not isinstance(content, list):
        return texts
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("input_text", "text"):
            text = _str(part.get("text"))
        elif kind in ("input_audio", "audio"):
            text = _str(part.get("transcript"))
        else:
            text = ""
        if text.strip():
            texts.append(text)
    return texts


class _Bounded(OrderedDict):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit

    def put(self, key: Any, value: Any) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > self._limit:
            self.popitem(last=False)


class RealtimeSessionRecorder:
    """1 つのリアルタイムのセッションで行き来する事象を受け、監査の事象へ写す。

    送信と受信は別の流れから呼ばれうるため、状態の更新は錠の内側で行う。送出は錠の外で行う。
    """

    def __init__(
        self,
        *,
        sdk: str = "openai",
        agent_id: str | None = None,
        transcriber: Transcriber | None = None,
    ) -> None:
        cfg = get_config()
        self._sdk = sdk
        self._agent_id = agent_id
        self._transcriber = transcriber
        # 1 つのセッションを 1 つの run に置く。明示された run があればそれに従う。
        self._run_id = get_run_id() or cfg.run_id or new_run_id()
        self._lock = threading.Lock()
        self._model: str | None = None
        self._tools: list[str] = []
        self._server_transcribes: bool | None = None
        self._format: _Format = _DEFAULT_FORMAT
        self._turn_id: str | None = None
        self._item_turns: _Bounded = _Bounded(_MAX_ITEMS)
        self._seen_calls: _Bounded = _Bounded(_MAX_CALLS)
        self._call_names: _Bounded = _Bounded(_MAX_CALLS)
        self._last_call_name: str = ""
        self._audio = bytearray()
        self._audio_truncated = False
        self._pool: ThreadPoolExecutor | None = None
        self._pending: list[Any] = []

    # ------------------------------------------------------------------ 入口

    def on_client_event(self, event: Any) -> None:
        """アプリが提供元へ送った事象を受ける。"""
        kind = _event_type(event)
        if kind not in _CLIENT_TYPES:
            return
        with audit_guard(f"realtime.client.{kind}"):
            if kind == "input_audio_buffer.append":
                self._append_audio(event)
                return
            if kind == "input_audio_buffer.clear":
                self._clear_audio()
                return
            payload = _as_dict(event)
            if payload is None:
                return
            if kind == "session.update":
                self._on_session_update(payload)
            elif kind == "conversation.item.create":
                self._on_item_create(payload)

    def on_server_raw(self, message: Any) -> None:
        """提供元から届いた解析前の本文を受ける。読む種別の名前が現れなければ解析しない。"""
        if isinstance(message, str):
            raw = message.encode("utf-8", "ignore")
        elif isinstance(message, (bytes, bytearray)):
            raw = bytes(message)
        else:
            self.on_server_event(message)
            return
        if not any(marker in raw for marker in _SERVER_MARKERS):
            return
        payload = _as_dict(raw)
        if payload is not None:
            self.on_server_event(payload)

    def on_server_event(self, event: Any) -> None:
        """提供元から届いた事象を受ける。"""
        kind = _event_type(event)
        if kind not in _SERVER_TYPES:
            return
        with audit_guard(f"realtime.server.{kind}"):
            payload = _as_dict(event)
            if payload is None:
                return
            if kind in ("session.created", "session.updated"):
                session = payload.get("session")
                if isinstance(session, dict):
                    self._absorb_session(session)
            elif kind == "input_audio_buffer.committed":
                self._on_committed(_str(payload.get("item_id")))
            elif kind == "input_audio_buffer.cleared":
                self._clear_audio()
            elif kind == "conversation.item.input_audio_transcription.completed":
                # 提供元が文字起こしを返している。以後、手元で起こさない。
                with self._lock:
                    self._server_transcribes = True
                self._emit_utterance(
                    _str(payload.get("transcript")),
                    item_id=_str(payload.get("item_id")),
                    origin="server_transcription",
                )
            elif kind == "response.function_call_arguments.done":
                self._on_function_call(
                    _str(payload.get("call_id")),
                    _str(payload.get("name")),
                    payload.get("arguments"),
                )
            elif kind == "response.output_item.done":
                item = payload.get("item")
                if isinstance(item, dict):
                    if item.get("type") == "function_call":
                        self._on_function_call(
                            _str(item.get("call_id")), _str(item.get("name")), item.get("arguments")
                        )
                    elif item.get("type") == "mcp_call":
                        self._on_mcp_call(item)

    def on_handoff(self, from_agent: Any, to_agent: Any) -> None:
        """裏のモデルの引き継ぎを、判断の事象として送る。"""
        with audit_guard("realtime.handoff"):
            handoff: dict[str, Any] = {
                "from_agent": _str(_get(from_agent, "name")),
                "to_agent": _str(_get(to_agent, "name")),
            }
            with self._lock:
                tool = self._last_call_name
            if tool:
                handoff["tool_name"] = tool
            self._emit(
                "agent.decision",
                {"decision_kind": "handoff", "handoff": handoff},
                operation="realtime.handoff",
            )

    def flush(self, timeout: float | None = None) -> None:
        """手元の文字起こしの完了を待つ。テストと終了の処理が使う。"""
        with self._lock:
            pending = list(self._pending)
            self._pending.clear()
        for future in pending:
            with contextlib.suppress(Exception):
                future.result(timeout=timeout)

    # ------------------------------------------------------------------ セッション

    def _absorb_session(self, session: dict[str, Any]) -> None:
        with self._lock:
            model = session.get("model")
            if isinstance(model, str) and model:
                self._model = model
            names = _session_tool_names(session)
            if names is not None:
                self._tools = names
            setting = _transcription_setting(session)
            if setting is not None:
                self._server_transcribes = setting
            fmt = _input_format(session)
            if fmt is not None:
                self._format = fmt

    def _on_session_update(self, payload: dict[str, Any]) -> None:
        session = payload.get("session")
        if not isinstance(session, dict):
            return
        self._absorb_session(session)
        instructions = session.get("instructions")
        if not isinstance(instructions, str):
            return
        self._emit_instructions(instructions, operation="realtime.session.update", voice=session.get("voice"))

    def _emit_instructions(self, instructions: str, *, operation: str, voice: Any = None) -> None:
        cfg = get_config()
        llm: dict[str, Any] = {
            "provider": "openai",
            "operation": operation,
            "model": self._model,
            "input": {"instructions": instructions} if cfg.capture_prompt else {"input_hash": sha256_value(instructions)},
        }
        lines = system_prompt_line_digests(instructions)
        pairs = system_prompt_pair_digests(instructions)
        if lines:
            llm["system_prompt_line_hashes"] = lines
        if pairs:
            llm["system_prompt_pair_hashes"] = pairs
        # 語の組が作れない本文のための文の署名。他の計装と同じく、鍵が無い構成では空を返す。
        semantic = system_prompt_semantic_digests(instructions)
        if semantic:
            llm["system_prompt_semantic_hashes"] = semantic
        realtime: dict[str, Any] = {"kind": "instructions"}
        if isinstance(voice, str) and voice:
            realtime["voice"] = voice
        llm["realtime"] = realtime
        self._emit("llm.request", {"llm": llm}, operation=operation)

    # ------------------------------------------------------------------ 発話

    def _start_turn(self, item_id: str) -> str:
        with self._lock:
            known = self._item_turns.get(item_id) if item_id else None
            if known is not None:
                # 既に始めた発話の turn。遅れて届いた文字起こしで、今の turn を巻き戻さない。
                return known
            turn = new_turn_id()
            if item_id:
                self._item_turns.put(item_id, turn)
            self._turn_id = turn
            return turn

    def _on_item_create(self, payload: dict[str, Any]) -> None:
        item = payload.get("item")
        if not isinstance(item, dict):
            return
        kind = item.get("type")
        if kind == "function_call_output":
            self._on_function_output(_str(item.get("call_id")), item.get("output"))
            return
        if kind != "message":
            return
        role = item.get("role")
        texts = _user_texts(item)
        if not texts:
            return
        text = "\n".join(texts)
        if role == "user":
            self._emit_utterance(text, item_id=_str(item.get("id")), origin="text")
        elif role == "system":
            # 会話の途中に差し込まれた指示。セッションの指示と同じ欄へ写す。
            self._emit_instructions(text, operation="realtime.conversation.item.create")

    def _emit_utterance(self, transcript: str, *, item_id: str, origin: str, truncated: bool = False) -> None:
        if not transcript.strip():
            return
        turn = self._start_turn(item_id)
        cfg = get_config()
        llm: dict[str, Any] = {
            "provider": "openai",
            "operation": "realtime.input_audio_transcription" if origin != "text" else "realtime.conversation.item.create",
            "model": self._model,
            "input": {"transcript": transcript} if cfg.capture_prompt else {"input_hash": sha256_value(transcript)},
        }
        if cfg.scan_input:
            llm.update(input_scan_fields(transcript))
        realtime: dict[str, Any] = {"kind": "utterance", "origin": origin}
        if item_id:
            realtime["item_id"] = item_id
        if truncated:
            realtime["input_audio_truncated"] = True
        llm["realtime"] = realtime
        self._emit("llm.request", {"llm": llm}, operation=llm["operation"], turn_id=turn)

    # ------------------------------------------------------------------ 手元の文字起こし

    def _local_active(self) -> Transcriber | None:
        transcriber = self._transcriber or _local_transcriber
        # 提供元が文字起こしを返さないと確定したときだけ使う。判別できないうちに起こすと、届いた
        # 文字起こしと同じ発話が 2 回送られる。
        if transcriber is None or self._server_transcribes is not False:
            return None
        return transcriber

    def _append_audio(self, event: Any) -> None:
        with self._lock:
            if self._local_active() is None:
                return
            audio = _get(event, "audio")
            if not isinstance(audio, str) or not audio:
                return
            if len(self._audio) >= _MAX_AUDIO_BYTES:
                self._audio_truncated = True
                return
            try:
                chunk = base64.b64decode(audio, validate=False)
            except Exception:  # noqa: BLE001 - 復号できない音声は溜めない
                return
            room = _MAX_AUDIO_BYTES - len(self._audio)
            if len(chunk) > room:
                self._audio_truncated = True
                chunk = chunk[:room]
            self._audio.extend(chunk)

    def _clear_audio(self) -> None:
        with self._lock:
            self._audio = bytearray()
            self._audio_truncated = False

    def _on_committed(self, item_id: str) -> None:
        self._start_turn(item_id)
        with self._lock:
            transcriber = self._local_active()
            audio = bytes(self._audio)
            truncated = self._audio_truncated
            self._audio = bytearray()
            self._audio_truncated = False
            fmt = self._format
            if transcriber is None or not audio:
                return
            if self._pool is None:
                # 文字起こしは重い。受信の流れを止めないよう、1 本の作業の流れへ渡す。
                self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="senda-realtime-stt")
            pool = self._pool
        ctx = copy_context()

        def work() -> None:
            with audit_guard("realtime.local_transcription"):
                text = transcriber(audio, fmt)
                if isinstance(text, str) and text.strip():
                    self._emit_utterance(text, item_id=item_id, origin="local_transcription", truncated=truncated)

        future = pool.submit(ctx.run, work)
        with self._lock:
            self._pending.append(future)
            del self._pending[:-_MAX_ITEMS]

    # ------------------------------------------------------------------ ツール

    def _on_function_call(self, call_id: str, name: str, arguments: Any) -> None:
        if not name:
            return
        with self._lock:
            if call_id:
                if call_id in self._seen_calls:
                    return
                self._seen_calls.put(call_id, True)
                self._call_names.put(call_id, name)
            self._last_call_name = name
            offered = list(self._tools)
        cfg = get_config()
        tool: dict[str, Any] = {"framework": FRAMEWORK, "tool_name": name, "arguments_hash": sha256_value(arguments)}
        if call_id:
            tool["call_id"] = call_id
        if cfg.capture_arguments:
            tool["arguments"] = arguments
        self._emit("tool_call.requested", {"tool": tool}, operation="realtime.function_call", status="start")
        if offered:
            self._emit(
                "agent.decision",
                {"alternatives": offered_alternatives(offered), "selected_tool": name},
                operation="realtime.function_call",
            )

    def _on_function_output(self, call_id: str, output: Any) -> None:
        with self._lock:
            name = self._call_names.get(call_id) if call_id else None
        cfg = get_config()
        tool: dict[str, Any] = {"framework": FRAMEWORK, "result_hash": sha256_value(output)}
        # 名前の分からない結果は名前を載せない。既定の名前へ倒すと、別の tool を呼んだことになる。
        if name:
            tool["tool_name"] = name
        if call_id:
            tool["call_id"] = call_id
        if cfg.capture_result:
            tool["result"] = output
        if cfg.scan_result:
            tool.update(result_scan_fields(output))
        self._emit("tool_call.completed", {"tool": tool}, operation="realtime.function_call_output")

    def _on_mcp_call(self, item: dict[str, Any]) -> None:
        call_id = _str(item.get("id"))
        name = _str(item.get("name"))
        if not name:
            return
        with self._lock:
            key = f"mcp:{call_id}" if call_id else ""
            if key:
                if key in self._seen_calls:
                    return
                self._seen_calls.put(key, True)
        cfg = get_config()
        arguments = item.get("arguments")
        parsed = _as_dict(arguments) if isinstance(arguments, str) else arguments
        output = item.get("output")
        error = item.get("error")
        mcp: dict[str, Any] = {
            "server": _str(item.get("server_label")) or None,
            "operation": "call_tool",
            "tool": name,
            "arguments_hash": sha256_value(arguments),
            "result_hash": sha256_value(output),
            "is_error": bool(error),
        }
        written = classify_instruction_write(parsed) if isinstance(parsed, dict) else None
        if written:
            mcp.update(written)
        if cfg.capture_arguments:
            mcp["arguments"] = parsed if parsed is not None else arguments
        if cfg.capture_result and output is not None:
            mcp["result"] = output
        if cfg.scan_result and output is not None:
            mcp.update(result_scan_fields(output))
        self._emit(
            "mcp.tool_call.failed" if error else "mcp.tool_call.completed",
            {"mcp": mcp},
            operation="realtime.mcp_call",
            status="error" if error else "success",
        )

    # ------------------------------------------------------------------ 送出

    def _emit(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        operation: str,
        status: str = "success",
        turn_id: str | None = None,
    ) -> None:
        turn = turn_id or self._turn_id or new_turn_id()
        run_token = set_run_id(self._run_id)
        turn_token = set_turn_id(turn)
        try:
            emit_event(
                event_type,
                source={"component": "instrumentor", "sdk": self._sdk, "provider": "openai", "operation": operation},
                data=data,
                status=status,
                agent_id=self._agent_id,
            )
        finally:
            reset_turn_id(turn_token)
            reset_run_id(run_token)


_RECORDER_ATTR = "_senda_realtime_recorder"


def recorder_for(holder: Any, *, sdk: str) -> RealtimeSessionRecorder:
    """接続や模型の実体ごとに 1 つの記録係を持たせる。"""
    recorder = getattr(holder, _RECORDER_ATTR, None)
    if not isinstance(recorder, RealtimeSessionRecorder):
        recorder = RealtimeSessionRecorder(sdk=sdk)
        with contextlib.suppress(Exception):
            setattr(holder, _RECORDER_ATTR, recorder)
    return recorder


def _observe(fn: Callable[[], None]) -> None:
    # 観測は本来の送受信から隔離する。記録係の中でも守っているが、記録係を得る段の失敗もここで止める。
    with contextlib.suppress(Exception):
        fn()


class OpenAIRealtimeInstrumentor(BaseInstrumentor):
    """OpenAI の SDK と Agents SDK のリアルタイムのセッションを包む。"""

    name = "openai_realtime"

    def __init__(self, transcriber: Transcriber | None = None) -> None:
        self._patches: list[tuple[Any, str, Callable[..., Any]]] = []
        if transcriber is not None:
            set_local_transcriber(transcriber)

    def instrument(self) -> bool:
        patched = False
        with contextlib.suppress(Exception):
            patched |= self._instrument_openai()
        with contextlib.suppress(Exception):
            patched |= self._instrument_agents()
        return patched

    def _patch(self, cls: Any, method: str, make: Callable[[Callable[..., Any]], Callable[..., Any]]) -> bool:
        original = getattr(cls, method, None)
        if original is None or hasattr(original, "__senda_patched__"):
            return False
        wrapped = make(original)
        wrapped.__senda_patched__ = True
        setattr(cls, method, wrapped)
        self._patches.append((cls, method, original))
        return True

    def _instrument_openai(self) -> bool:
        try:
            from openai.resources.realtime import realtime as rt  # type: ignore
        except Exception:  # noqa: BLE001 - 任意の SDK の import 失敗は種類を問わず未導入として扱う
            return False
        patched = False
        sync_conn = getattr(rt, "RealtimeConnection", None)
        async_conn = getattr(rt, "AsyncRealtimeConnection", None)
        if sync_conn is not None:
            patched |= self._patch(sync_conn, "send", _sync_send)
            patched |= self._patch(sync_conn, "send_raw", _sync_send)
            patched |= self._patch(sync_conn, "recv_bytes", _sync_recv_bytes)
        if async_conn is not None:
            patched |= self._patch(async_conn, "send", _async_send)
            patched |= self._patch(async_conn, "send_raw", _async_send)
            patched |= self._patch(async_conn, "recv_bytes", _async_recv_bytes)
        return patched

    def _instrument_agents(self) -> bool:
        try:
            from agents.realtime import openai_realtime as model_mod  # type: ignore
            from agents.realtime import session as session_mod  # type: ignore
        except Exception:  # noqa: BLE001 - 任意の SDK の import 失敗は種類を問わず未導入として扱う
            return False
        patched = False
        model_cls = getattr(model_mod, "OpenAIRealtimeWebSocketModel", None)
        if model_cls is not None:
            patched |= self._patch(model_cls, "_send_raw_message", _agents_send)
            patched |= self._patch(model_cls, "_send_raw_message_if", _agents_send_if)
            patched |= self._patch(model_cls, "_handle_ws_event", _agents_handle)
        session_cls = getattr(session_mod, "RealtimeSession", None)
        if session_cls is not None:
            patched |= self._patch(session_cls, "_put_event", _agents_put_event)
        return patched

    def uninstrument(self) -> bool:
        for cls, method, original in self._patches:
            setattr(cls, method, original)
        self._patches = []
        return True


_SDK_OPENAI = "openai"
_SDK_AGENTS = "openai_agents"


def _sync_send(original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(self, event, *args, **kwargs)
        _observe(lambda: recorder_for(self, sdk=_SDK_OPENAI).on_client_event(_client_event(event)))
        return result

    return wrapper


def _async_send(original: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapper(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original(self, event, *args, **kwargs)
        _observe(lambda: recorder_for(self, sdk=_SDK_OPENAI).on_client_event(_client_event(event)))
        return result

    return wrapper


def _client_event(event: Any) -> Any:
    # send_raw は文字列か本文で受ける。種別を見られるよう、種別の名前が現れるときだけ解析する。
    if isinstance(event, (bytes, bytearray, str)):
        raw = event.encode("utf-8", "ignore") if isinstance(event, str) else bytes(event)
        if any(t.encode() in raw for t in _CLIENT_TYPES):
            return _as_dict(raw) or {}
        return {}
    return event


def _sync_recv_bytes(original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        message = original(self, *args, **kwargs)
        _observe(lambda: recorder_for(self, sdk=_SDK_OPENAI).on_server_raw(message))
        return message

    return wrapper


def _async_recv_bytes(original: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        message = await original(self, *args, **kwargs)
        _observe(lambda: recorder_for(self, sdk=_SDK_OPENAI).on_server_raw(message))
        return message

    return wrapper


def _agents_send(original: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapper(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original(self, event, *args, **kwargs)
        _observe(lambda: recorder_for(self, sdk=_SDK_AGENTS).on_client_event(event))
        return result

    return wrapper


def _agents_send_if(original: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapper(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
        sent = await original(self, event, *args, **kwargs)
        # 条件が成り立たず送らなかった事象は記録しない。
        if sent:
            _observe(lambda: recorder_for(self, sdk=_SDK_AGENTS).on_client_event(event))
        return sent

    return wrapper


def _agents_handle(original: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapper(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
        _observe(lambda: recorder_for(self, sdk=_SDK_AGENTS).on_server_event(event))
        return await original(self, event, *args, **kwargs)

    return wrapper


def _agents_put_event(original: Callable[..., Any]) -> Callable[..., Any]:
    async def wrapper(self: Any, event: Any, *args: Any, **kwargs: Any) -> Any:
        if _event_type(event) == "handoff":
            model = getattr(self, "_model", None)
            if model is not None:
                _observe(
                    lambda: recorder_for(model, sdk=_SDK_AGENTS).on_handoff(
                        getattr(event, "from_agent", None), getattr(event, "to_agent", None)
                    )
                )
        return await original(self, event, *args, **kwargs)

    return wrapper

