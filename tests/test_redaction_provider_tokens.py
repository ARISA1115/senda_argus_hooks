"""値ベースの秘匿が主要プロバイダのトークン形式を捕捉することの確認。

固定キー集合に一致しない引数の値として資格情報が渡っても、redact_value が主要プロバイダの
トークン(GitHub / Slack / Google / JWT 等)を平文で通さない。redact_event を経たイベントが
検知ストアへ平文トークンを残さないための送出側の担保。
"""

import pytest

from senda_argus_hooks.core.redaction import redact_value, redact_event

REDACTED = "***REDACTED***"

# フェイクトークンは接頭辞と本体を分割構築する。連続リテラルにすると GitHub の secret scanning /
# push protection が実トークンと誤検知してブロックするため、source には秘匿対象の連続文字列を
# 残さない。実行時に連結した値が redaction 正規表現に一致するかだけを検証する。
_TOKENS = [
    ("openai_sk", "sk-" + "0" * 24),
    ("aws_akia", "AKIA" + "0" * 16),
    ("github_pat", "ghp_" + "0" * 36),
    ("github_oauth", "gho_" + "0" * 36),
    ("github_fine", "github_pat_" + "0" * 30),
    ("slack_bot", "xox" + "b-" + "0" * 24),
    ("google_apikey", "AIza" + "0" * 35),
    ("google_oauth", "ya29." + "0" * 30),
    ("jwt", "eyJ" + "0" * 12 + "." + "0" * 12 + "." + "0" * 20),
]


@pytest.mark.parametrize("name,token", _TOKENS)
def test_provider_token_is_redacted_as_bare_value(name, token):
    # キー名に依らず、値として現れたトークンが平文で残らない。
    redacted = redact_value({"note": f"credential is {token} for the call"})
    assert token not in redacted["note"], name
    assert REDACTED in redacted["note"], name


@pytest.mark.parametrize("name,token", _TOKENS)
def test_provider_token_redacted_in_nested_args(name, token):
    payload = {"tool": "call", "args": {"headers": [f"X: {token}"]}}
    redacted = redact_value(payload)
    assert token not in str(redacted), name


def test_bearer_keeps_prefix_and_redacts_secret():
    # bearer は接頭辞を残し以降を伏せる従来挙動を保つ。
    out = redact_value("Authorization: Bearer abc.def-123_XYZ")
    assert "Bearer " in out
    assert "abc.def-123_XYZ" not in out


def test_redact_event_marks_and_hides_tokens():
    secret = "sk-" + "0" * 24  # 連続リテラルを避けたフェイク値
    event = {"data": {"llm": {"input": {"prompt": f"key {secret}"}}}}
    out = redact_event(event)
    assert out["security"]["redacted"] is True
    assert secret not in str(out)


def test_non_token_text_is_untouched():
    # 通常のテキストは伏せない (過剰秘匿しない)。
    out = redact_value({"msg": "hello world, list the files please"})
    assert out["msg"] == "hello world, list the files please"
