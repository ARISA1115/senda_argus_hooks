"""観測の後処理が失敗しても本来の呼び出しに波及しないことの検証。

計装は本番の推論経路に対して安全側に倒す。呼び出しが成功した後のイベント送出や
ペイロード組み立てが失敗しても、呼び出し元へは応答をそのまま返す。呼び出し自体の
失敗は従来どおり送出する。
"""

import sys
import types

import pytest

from senda_argus_hooks import register, shutdown
from senda_argus_hooks.instrumentors.base import audit_guard


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self):
        return self.payload


def _install_fake_openai(monkeypatch):
    class Completions:
        def create(self, *args, **kwargs):
            if kwargs.get("fail"):
                raise RuntimeError("fake openai failure")
            return _Response({"id": "chatcmpl_fake", "model": kwargs.get("model")})

    class Responses:
        def create(self, *args, **kwargs):
            return _Response({"id": "resp_fake", "model": kwargs.get("model")})

    class Embeddings:
        def create(self, *args, **kwargs):
            return _Response({"id": "emb_fake", "model": kwargs.get("model")})

    fake_openai = types.ModuleType("openai")
    fake_openai.resources = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(Completions=Completions)),
        responses=types.SimpleNamespace(Responses=Responses),
        embeddings=types.SimpleNamespace(Embeddings=Embeddings),
    )
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    return Completions


class TestAuditGuard:
    def test_swallows_exception(self):
        with audit_guard("test.operation"):
            raise RuntimeError("observation failed")

    def test_does_not_swallow_keyboard_interrupt(self):
        with pytest.raises(KeyboardInterrupt):
            with audit_guard("test.operation"):
                raise KeyboardInterrupt

    def test_does_not_swallow_system_exit(self):
        with pytest.raises(SystemExit):
            with audit_guard("test.operation"):
                raise SystemExit(1)

    def test_normal_block_runs_to_completion(self):
        seen = []
        with audit_guard("test.operation"):
            seen.append("ran")
        assert seen == ["ran"]


class TestOpenAIWrapperIsolation:
    def _register(self, tmp_path):
        return register(
            project="test-isolation",
            exporters=[{"type": "jsonl", "path": str(tmp_path / "events.jsonl")}],
            auto_instrument=True,
            instrument_anthropic=False,
            instrument_litellm=False,
            instrument_mcp=False,
            instrument_argus_sdk=False,
            capture_prompt=False,
            capture_response=False,
        )

    def test_emit_failure_does_not_break_the_call(self, tmp_path, monkeypatch):
        Completions = _install_fake_openai(monkeypatch)
        self._register(tmp_path)
        try:
            # 観測の送出だけを失敗させる
            import senda_argus_hooks.instrumentors.openai as inst

            def _boom(*args, **kwargs):
                raise RuntimeError("emit failed")

            monkeypatch.setattr(inst, "emit_event", _boom)

            response = Completions().create(model="gpt-fake", messages=[])
            # 観測が失敗しても応答はそのまま返る
            assert response.model_dump()["model"] == "gpt-fake"
        finally:
            shutdown()

    def test_payload_failure_does_not_break_the_call(self, tmp_path, monkeypatch):
        Completions = _install_fake_openai(monkeypatch)
        self._register(tmp_path)
        try:
            import senda_argus_hooks.instrumentors.openai as inst

            def _boom(*args, **kwargs):
                raise RuntimeError("payload build failed")

            monkeypatch.setattr(inst, "_safe_response", _boom)

            response = Completions().create(model="gpt-fake", messages=[])
            assert response.model_dump()["model"] == "gpt-fake"
        finally:
            shutdown()

    def test_call_failure_still_propagates(self, tmp_path, monkeypatch):
        Completions = _install_fake_openai(monkeypatch)
        self._register(tmp_path)
        try:
            with pytest.raises(RuntimeError, match="fake openai failure"):
                Completions().create(model="gpt-fake", messages=[], fail=True)
        finally:
            shutdown()


class TestGuardSurvivesLoggingFailure:
    """記録そのものが失敗してもガードが破れないことの検証。

    ログの送り先が落ちている環境でも、観測の失敗を本来の呼び出しへ波及させない。
    """

    def test_logger_raising_does_not_escape(self, monkeypatch):
        import senda_argus_hooks.instrumentors.base as base

        def _boom(*args, **kwargs):
            raise RuntimeError("log sink down")

        monkeypatch.setattr(base.logger, "warning", _boom)

        with base.audit_guard("test.operation"):
            raise RuntimeError("observation failed")

    def test_logger_raising_does_not_break_instrumented_call(self, tmp_path, monkeypatch):
        Completions = _install_fake_openai(monkeypatch)
        register(
            project="test-log-failure",
            exporters=[{"type": "jsonl", "path": str(tmp_path / "events.jsonl")}],
            auto_instrument=True,
            instrument_anthropic=False,
            instrument_litellm=False,
            instrument_mcp=False,
            instrument_argus_sdk=False,
            capture_prompt=False,
            capture_response=False,
        )
        try:
            import senda_argus_hooks.instrumentors.base as base
            import senda_argus_hooks.instrumentors.openai as inst

            def _emit_boom(*args, **kwargs):
                raise RuntimeError("emit failed")

            def _log_boom(*args, **kwargs):
                raise RuntimeError("log sink down")

            monkeypatch.setattr(inst, "emit_event", _emit_boom)
            monkeypatch.setattr(base.logger, "warning", _log_boom)

            response = Completions().create(model="gpt-fake", messages=[])
            assert response.model_dump()["model"] == "gpt-fake"
        finally:
            shutdown()
