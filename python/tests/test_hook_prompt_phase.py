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
