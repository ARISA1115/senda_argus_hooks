"""scan-mcp のコマンドと、MCP の設定の読み取りのテスト。偽の走査器と偽の送信先を使う。"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from senda_argus_hooks.cli import mcp_configs, scan_mcp
from senda_argus_hooks.cli.agent_hooks_install import INSTALLS
from senda_argus_hooks.cli.main import main
from senda_argus_hooks.cli.mcp_configs import read_configured_servers


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    # 実行したディレクトリの設定を読むため、テストごとに空のディレクトリで動かす。
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


# 秘密の値は分割して組み立て、ファイルに秘密の形の文字列を直書きしない。
GH_TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
ENV_SECRET = "env-" + "value-" + "do-not-send-7731"
HEADER_SECRET = "hdr-" + "value-" + "do-not-send-9921"
# 名前の旗の次に置く値。値の形だけでは秘密に見えない字面にし、旗の次を除く処理だけで消えることを確認する。
FLAG_SECRET = "flag" + "value" + "-do-not-send"
URL_PASS = "pw" + "DoNotSend5521"
QUERY_SECRET = "qs" + "DoNotSend6631"


def _write(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _gemini(home: Path, servers: dict) -> Path:
    return _write(
        home / ".gemini" / "settings.json", {"theme": "x", "mcpServers": servers}
    )


def _crush(home: Path, servers: dict) -> Path:
    return _write(home / ".config" / "crush" / "crush.json", {"mcp": servers})


def _run(home: Path, *extra: str, capsys) -> tuple[int, dict]:
    code = main(
        [
            "scan-mcp",
            "--home",
            str(home),
            "--system-root",
            str(home / "sysroot"),
            "--platform",
            "linux",
            *extra,
        ]
    )
    out = capsys.readouterr().out
    return code, json.loads(out)


@pytest.fixture
def no_scanner(monkeypatch, tmp_path):
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("SENDA_ARGUS_ENDPOINT", raising=False)


FAKE_SCANNER = r"""#!{python}
import json, os, sys, time
mode = os.environ.get("FAKE_SCANNER_MODE", "ok")
with open(os.environ["FAKE_SCANNER_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
if mode == "sleep":
    time.sleep(5)
if mode == "garbage":
    print("not json")
    sys.exit(0)
if mode == "spew":
    sys.stdout.write("x" * 4096)
    sys.stdout.flush()
    time.sleep(5)
if mode == "fail":
    sys.exit(3)
tools = json.load(open(sys.argv[sys.argv.index("--tools") + 1]))["tools"]
print(json.dumps({{"server_url": "static", "scan_results": [
    {{"tool_name": t["name"], "status": "completed", "is_safe": False, "analyzer_errors": [],
      "findings": {{"yara_analyzer": {{"severity": "HIGH", "threat_names": ["PROMPT INJECTION"], "total_findings": 1}}}}}}
    for t in tools]}}))
"""


@pytest.fixture
def fake_scanner(monkeypatch, tmp_path):
    import sys

    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "mcp-scanner"
    exe.write_text(FAKE_SCANNER.format(python=sys.executable), encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "scanner.log"
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("FAKE_SCANNER_LOG", str(log))
    monkeypatch.delenv("SENDA_ARGUS_ENDPOINT", raising=False)
    return log


def _tools_file(tmp_path: Path, names=("read_file",)) -> Path:
    return _write(
        tmp_path / "tools.json",
        {
            "tools": [
                {"name": n, "description": "d", "inputSchema": {"type": "object"}}
                for n in names
            ]
        },
    )


# --- 設定の読み取り -----------------------------------------------------------------------


def test_reads_only_products_whose_table_file_holds_mcp_definitions(tmp_path):
    home = tmp_path
    _gemini(home, {"g": {"command": "npx", "args": ["-y", "pkg"]}})
    _crush(home, {"c": {"type": "stdio", "command": "uvx", "args": ["tool"]}})
    # hook だけの置き場所に mcpServers があっても読まない。表の置き場所が MCP の定義のファイルでない製品。
    _write(
        home / ".claude" / "settings.json", {"mcpServers": {"x": {"command": "evil"}}}
    )
    read = read_configured_servers(
        platform="linux", home=home, system_root=home / "sys"
    )
    assert sorted((s.product, s.name) for s in read.servers) == [
        ("crush", "c"),
        ("gemini_cli", "g"),
    ]
    without_sources = {p for p, spec in INSTALLS.items() if not spec.mcp_sources}
    assert without_sources <= set(read.refused_products)
    assert all(read.refused_products[p] for p in read.refused_products)
    # 読む物が無いだけでは、読み取りの失敗にしない。
    assert read.unreadable_products == {}


def test_read_does_not_modify_config(tmp_path):
    path = _gemini(
        tmp_path, {"g": {"command": "npx", "args": ["-y", "pkg"], "env": {"K": "v"}}}
    )
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    read_configured_servers(
        platform="linux", home=tmp_path, system_root=tmp_path / "sys"
    )
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime


def test_unreadable_file_is_listed_with_reason_and_other_products_still_read(tmp_path):
    p = tmp_path / ".gemini" / "settings.json"
    p.parent.mkdir(parents=True)
    p.write_text("{broken", encoding="utf-8")
    _crush(tmp_path, {"c": {"type": "stdio", "command": "uvx", "args": ["tool"]}})
    read = read_configured_servers(
        platform="linux", home=tmp_path, system_root=tmp_path / "sys"
    )
    assert "JSON" in read.unreadable_products["gemini_cli"]
    assert [s.name for s in read.servers] == ["c"]


def test_managed_scope_is_read_from_table_location(tmp_path):
    sysroot = tmp_path / "sys"
    _write(
        sysroot / "etc" / "gemini-cli" / "settings.json",
        {"mcpServers": {"m": {"command": "node", "args": ["/x.js"]}}},
    )
    read = read_configured_servers(platform="linux", home=tmp_path, system_root=sysroot)
    assert [(s.scope, s.name) for s in read.servers] == [("managed", "m")]


def test_servers_over_cap_are_reported_not_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_configs, "MAX_SERVERS_PER_FILE", 2)
    _gemini(tmp_path, {f"s{i}": {"command": "npx", "args": ["p"]} for i in range(3)})
    read = read_configured_servers(
        platform="linux", home=tmp_path, system_root=tmp_path / "sys"
    )
    assert len(read.servers) == 2
    assert [s["name"] for s in read.skipped_servers] == ["s2"]


# --- 同じサーバの一覧 ---------------------------------------------------------------------


def test_scan_targets_equal_configured_servers(tmp_path, capsys, no_scanner):
    _gemini(
        tmp_path,
        {
            "a": {"command": "npx", "args": ["-y", "@scope/a@1.0.0"]},
            "r": {"httpUrl": "https://mcp.example.com/mcp"},
        },
    )
    _crush(
        tmp_path,
        {
            "a": {"type": "stdio", "command": "uvx", "args": ["a-tool==2.0"]},
            "h": {"type": "http", "url": "https://h.example.com/"},
        },
    )
    read = read_configured_servers(
        platform="linux", home=tmp_path, system_root=tmp_path / "sysroot"
    )
    code, out = _run(tmp_path, capsys=capsys)
    assert code == 0
    assert out["servers_read"] == len(read.servers) == 4
    sent = [(r["product"], r["body"]["artifact"]["name"]) for r in out["items"]]
    assert sent == [(s.product, s.name) for s in read.servers]


def test_same_name_different_entity_are_separate_items(tmp_path, capsys, no_scanner):
    _gemini(tmp_path, {"fs": {"command": "npx", "args": ["-y", "safe-fs@1.0.0"]}})
    _crush(
        tmp_path,
        {"fs": {"type": "stdio", "command": "npx", "args": ["-y", "evil-fs@6.6.6"]}},
    )
    _, out = _run(tmp_path, capsys=capsys)
    arts = [r["body"]["artifact"] for r in out["items"]]
    assert [a["package"] for a in arts] == ["safe-fs", "evil-fs"]
    assert arts[0]["name"] == arts[1]["name"] == "fs"


def test_ambiguous_tools_selector_is_refused(tmp_path, capsys, fake_scanner):
    _gemini(tmp_path, {"fs": {"command": "npx", "args": ["-y", "safe-fs@1.0.0"]}})
    _crush(
        tmp_path,
        {"fs": {"type": "stdio", "command": "npx", "args": ["-y", "evil-fs@6.6.6"]}},
    )
    tools = _tools_file(tmp_path)
    code = main(
        [
            "scan-mcp",
            "--home",
            str(tmp_path),
            "--system-root",
            str(tmp_path / "s"),
            "--platform",
            "linux",
            "--tools-json",
            f"fs={tools}",
        ]
    )
    assert code == 2
    assert "<製品>:<名前>" in capsys.readouterr().err
    assert not fake_scanner.exists()
    # 製品を付けた指定は 1 つの実体だけに当たる。
    _, out = _run(tmp_path, "--tools-json", f"crush:fs={tools}", capsys=capsys)
    by_product = {r["product"]: r["body"] for r in out["items"]}
    assert "report" in by_product["crush"]
    assert by_product["gemini_cli"]["unscanned_reason"] == "analyzer_disabled"


def test_unknown_tools_selector_is_refused(tmp_path, capsys, fake_scanner):
    _gemini(tmp_path, {"fs": {"command": "npx", "args": ["x"]}})
    code = main(
        [
            "scan-mcp",
            "--home",
            str(tmp_path),
            "--platform",
            "linux",
            "--tools-json",
            f"nope={_tools_file(tmp_path)}",
        ]
    )
    assert code == 2


# --- 導入物の実体 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "args", "package", "version"),
    [
        (
            "npx",
            ["-y", "@modelcontextprotocol/server-github@2025.4.8"],
            "@modelcontextprotocol/server-github",
            "2025.4.8",
        ),
        ("npx", ["-y", "plain-pkg"], "plain-pkg", ""),
        ("npx", ["--package", "real@1.2.0", "-y", "bin-name"], "real", "1.2.0"),
        ("uvx", ["mcp-server-fetch==0.6.2"], "mcp-server-fetch", "0.6.2"),
        ("uvx", ["--from", "git-tool[extra]@1.0", "git-cmd"], "git-tool", "1.0"),
        ("/usr/bin/uvx", ["--python", "3.12", "tool"], "tool", ""),
        ("pipx", ["run", "--spec", "pkg==3.1", "cmd"], "pkg", "3.1"),
        ("node", ["/srv/server.js"], "", ""),
        ("docker", ["run", "image"], "", ""),
    ],
)
def test_package_of_launch_shapes(command, args, package, version):
    assert scan_mcp.package_of(command, args) == (package, version)


def test_local_script_hash_only_for_existing_file(tmp_path, monkeypatch):
    # 作業ディレクトリに同じ名前のファイルを置き、作業ディレクトリで解かないことを確認する。
    monkeypatch.chdir(tmp_path)
    script = tmp_path / "server.js"
    script.write_bytes(b"console.log(1)")
    expected = hashlib.sha256(b"console.log(1)").hexdigest()
    assert scan_mcp.artifact_hash_of("node", [str(script)], None) == expected
    # 相対の場所は設定の cwd があるときだけ解く。プロセスの作業ディレクトリで推測しない。
    assert scan_mcp.artifact_hash_of("node", ["server.js"], None) is None
    assert scan_mcp.artifact_hash_of("node", ["server.js"], str(tmp_path)) == expected
    assert (
        scan_mcp.artifact_hash_of("node", [str(tmp_path / "missing.js")], None) is None
    )
    exe = tmp_path / "srv"
    exe.write_bytes(b"bin")
    assert (
        scan_mcp.artifact_hash_of(str(exe), [], None)
        == hashlib.sha256(b"bin").hexdigest()
    )
    # PATH から引くコマンドの名前は推測でファイルにしない。
    assert scan_mcp.artifact_hash_of("npx", ["-y", "x"], None) is None


def test_hash_over_cap_is_omitted(tmp_path, monkeypatch):
    monkeypatch.setattr(scan_mcp, "MAX_HASH_BYTES", 4)
    script = tmp_path / "big.js"
    script.write_bytes(b"12345")
    assert scan_mcp.artifact_hash_of("node", [str(script)], None) is None


def test_remote_url_is_normalized_without_credentials(tmp_path, capsys, no_scanner):
    _gemini(
        tmp_path,
        {
            "r": {
                "httpUrl": f"https://user:{URL_PASS}@MCP.Example.com:443/mcp/?key={QUERY_SECRET}",
                "headers": {"Authorization": HEADER_SECRET},
            }
        },
    )
    _, out = _run(tmp_path, capsys=capsys)
    art = out["items"][0]["body"]["artifact"]
    assert art == {
        "kind": "mcp_server",
        "name": "r",
        "transport": "remote",
        "url": "https://mcp.example.com/mcp",
    }
    assert out["items"][0]["header_keys"] == ["Authorization"]


def test_conflicting_transport_is_not_guessed(tmp_path, capsys, no_scanner):
    _gemini(
        tmp_path,
        {"both": {"command": "npx", "args": ["x"], "url": "https://a.example/"}},
    )
    code, out = _run(tmp_path, capsys=capsys)
    assert code == 1
    assert "body" not in out["items"][0]
    assert out["items"][0]["error"]


# --- 秘密の除去 ---------------------------------------------------------------------------


def test_secrets_are_not_in_body_or_display(tmp_path, capsys, no_scanner):
    _gemini(
        tmp_path,
        {
            "s": {
                "command": "npx",
                "args": [
                    "-y",
                    "@scope/srv@1.0.0",
                    "--token",
                    FLAG_SECRET,
                    f"--api-key={FLAG_SECRET}x",
                    GH_TOKEN,
                    f"https://u:{URL_PASS}@host.example/path?sig={QUERY_SECRET}",
                    "--header",
                    f"Authorization: Bearer {HEADER_SECRET}",
                    f"X-Api-Key:{HEADER_SECRET}y",
                    "--verbose",
                ],
                "env": {"GITHUB_TOKEN": ENV_SECRET},
            }
        },
    )
    _crush(
        tmp_path,
        {
            "h": {
                "type": "http",
                "url": "https://h.example/",
                "headers": {"Authorization": HEADER_SECRET},
            }
        },
    )
    code, out = _run(tmp_path, "--dry-run", capsys=capsys)
    text = json.dumps(out)
    for secret in (
        GH_TOKEN,
        ENV_SECRET,
        HEADER_SECRET,
        FLAG_SECRET,
        URL_PASS,
        QUERY_SECRET,
    ):
        assert secret not in text, secret
    item = out["items"][0]
    assert item["env_keys"] == ["GITHUB_TOKEN"]
    args = item["body"]["artifact"]["args"]
    # 秘密でない引数は保つ。パッケージの取り出しと実体の材料に要る。
    assert args[:2] == ["-y", "@scope/srv@1.0.0"]
    assert args[-1] == "--verbose"
    assert "https://<redacted>@host.example/path?<redacted>" in args
    assert item["redacted_args"] == 6
    assert code == 0


@pytest.mark.parametrize(
    "value",
    [
        # 32 文字に満たず、名前も秘密の形でない。値の形だけで当てる。
        "gh" + "p_" + "abcDEF123456",
        "Bearer " + "abc123",
        "AK" + "IA" + "ABCDEFGHIJ012345",
        "sk-" + "proj1234abcd",
        "xo" + "xb-" + "12345678-abcdefgh",
    ],
)
def test_short_tokens_are_redacted_by_value_shape(value):
    assert len(value) < 32
    assert scan_mcp.redact_arg(value) == scan_mcp.REDACTED


@pytest.mark.parametrize(
    "value",
    [
        "--region=asia-northeast1",
        "@modelcontextprotocol/server-filesystem",
        "/home/user/projects",
        "mcp-server-fetch==0.6.2",
        "--disk-cache",
        "https://example.com/sse",
    ],
)
def test_ordinary_args_are_kept(value):
    assert scan_mcp.redact_arg(value) == value


# --- 走査器 -------------------------------------------------------------------------------


def test_default_does_not_start_servers_or_scanner(
    tmp_path, capsys, fake_scanner, monkeypatch
):
    calls: list = []
    real_run = subprocess.run

    def spy(argv, *a, **k):
        calls.append(list(argv))
        return real_run(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", spy)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: pytest.fail("Popen called")
    )
    _gemini(
        tmp_path,
        {
            "s": {"command": "npx", "args": ["-y", "pkg"]},
            "r": {"url": "https://r.example/sse"},
        },
    )
    code, out = _run(tmp_path, capsys=capsys)
    assert calls == []
    assert not fake_scanner.exists()
    assert [r["body"]["unscanned_reason"] for r in out["items"]] == [
        "analyzer_disabled"
    ] * 2
    assert all("report" not in r["body"] for r in out["items"])
    assert code == 0


def test_scanner_runs_static_yara_only_with_global_flags_first(
    tmp_path, capsys, fake_scanner
):
    _gemini(tmp_path, {"s": {"command": "npx", "args": ["-y", "pkg"]}})
    _, out = _run(tmp_path, "--tools-json", f"s={_tools_file(tmp_path)}", capsys=capsys)
    argv = json.loads(fake_scanner.read_text().splitlines()[0])
    assert argv[:5] == ["--analyzers", "yara", "--format", "raw", "static"]
    assert argv[5] == "--tools"
    assert (
        "llm" not in " ".join(argv).lower()
        and "virustotal" not in " ".join(argv).lower()
    )
    body = out["items"][0]["body"]
    assert (
        body["report"]["scan_results"][0]["findings"]["yara_analyzer"]["severity"]
        == "HIGH"
    )
    assert set(body["tools"]) == {"read_file"}
    assert "unscanned_reason" not in body


def test_missing_scanner_marks_all_unscanned(tmp_path, capsys, no_scanner):
    _gemini(tmp_path, {"s": {"command": "npx", "args": ["-y", "pkg"]}})
    _, out = _run(tmp_path, "--tools-json", f"s={_tools_file(tmp_path)}", capsys=capsys)
    body = out["items"][0]["body"]
    assert body["unscanned_reason"] == "analyzer_disabled"
    assert "report" not in body and "tools" not in body


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("sleep", "timeout"),
        ("garbage", "analyzer_disabled"),
        ("fail", "analyzer_disabled"),
    ],
)
def test_scanner_failures_become_unscanned(
    tmp_path, capsys, fake_scanner, monkeypatch, mode, reason
):
    monkeypatch.setenv("FAKE_SCANNER_MODE", mode)
    _gemini(tmp_path, {"s": {"command": "npx", "args": ["-y", "pkg"]}})
    _, out = _run(
        tmp_path,
        "--timeout",
        "1",
        "--tools-json",
        f"s={_tools_file(tmp_path)}",
        capsys=capsys,
    )
    item = out["items"][0]
    assert item["body"]["unscanned_reason"] == reason
    assert "report" not in item["body"]
    assert item["scan_note"]


# --- 送信 ---------------------------------------------------------------------------------


class _Receiver:
    def __init__(self, status: int):
        self.status = status
        self.requests: list[tuple[str, dict, dict]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length))
                outer.requests.append(
                    (self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                )
                payload = json.dumps(
                    {"status": "stored", "artifact_key": "k"}
                    if outer.status == 200
                    else {"error": "scanner output format mismatch"}
                ).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver_factory():
    made: list[_Receiver] = []

    def make(status=200):
        r = _Receiver(status)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


def test_nothing_is_sent_without_send(
    tmp_path, capsys, no_scanner, receiver_factory, monkeypatch
):
    recv = receiver_factory()
    monkeypatch.setenv("SENDA_ARGUS_ENDPOINT", recv.url)
    _gemini(tmp_path, {"s": {"command": "npx", "args": ["-y", "pkg"]}})
    _run(tmp_path, capsys=capsys)
    _run(tmp_path, "--send", "--dry-run", capsys=capsys)
    assert recv.requests == []


def test_send_posts_every_item_with_existing_exporter_settings(
    tmp_path, capsys, fake_scanner, receiver_factory, monkeypatch
):
    recv = receiver_factory()
    monkeypatch.setenv("SENDA_ARGUS_ENDPOINT", recv.url)
    key = "collector-" + "key-" + "x1"
    monkeypatch.setenv("SENDA_ARGUS_API_KEY", key)
    _gemini(
        tmp_path,
        {
            "s": {
                "command": "npx",
                "args": ["-y", "pkg", "--token", FLAG_SECRET],
                "env": {"T": ENV_SECRET},
            },
            "r": {"url": "https://r.example/sse"},
        },
    )
    code, out = _run(
        tmp_path, "--send", "--tools-json", f"s={_tools_file(tmp_path)}", capsys=capsys
    )
    assert code == 0
    assert [p for p, _, _ in recv.requests] == ["/v1/install-scans"] * 2
    assert all(h.get("x-api-key") == key for _, h, _ in recv.requests)
    sent = json.dumps([b for _, _, b in recv.requests])
    assert FLAG_SECRET not in sent and ENV_SECRET not in sent
    assert key not in json.dumps(out)
    assert [r["response_status"] for r in out["items"]] == [200, 200]
    assert len(recv.requests) == out["servers_read"]


def test_receiver_rejection_is_shown_as_failure(
    tmp_path, capsys, fake_scanner, receiver_factory, monkeypatch
):
    recv = receiver_factory(status=422)
    monkeypatch.setenv("SENDA_ARGUS_ENDPOINT", recv.url)
    _gemini(tmp_path, {"s": {"command": "npx", "args": ["-y", "pkg"]}})
    code, out = _run(
        tmp_path, "--send", "--tools-json", f"s={_tools_file(tmp_path)}", capsys=capsys
    )
    assert code == 1
    assert out["items"][0]["response_status"] == 422
    assert out["items"][0]["response"]["error"] == "scanner output format mismatch"


def test_send_without_endpoint_fails(tmp_path, capsys, no_scanner):
    _gemini(tmp_path, {"s": {"command": "npx", "args": ["x"]}})
    assert (
        main(["scan-mcp", "--home", str(tmp_path), "--platform", "linux", "--send"])
        == 2
    )


def test_scan_does_not_use_new_environment_variables():
    source = Path(scan_mcp.__file__).read_text(encoding="utf-8")
    names = set(__import__("re").findall(r"environ\.get\(\"([A-Z_]+)\"", source))
    assert names <= {
        "SENDA_ARGUS_ENDPOINT",
        "SENDA_ARGUS_API_KEY",
        "SENDA_ARGUS_TIMEOUT",
    }
    assert os.path.basename(scan_mcp.__file__) == "scan_mcp.py"


# --- 監査の指摘への対応 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "60ff5a50" + "1c2d3e4f" + "5a6b7c8d" + "9e0f4eb4",
        "d98d4b7b" + "0123abcd" + "4567ef01" + "89ab4af4" + "6260aaaa",
        "56fb41b2-" + "f9ed-4da0-8f2d-" + "cbcd2f2c30de",
        "Ab3" + "xY9" + "kL2" + "mN7" + "pQ4" + "rS8" + "tU",
    ],
)
def test_opaque_shapes_without_names_are_redacted(value):
    assert scan_mcp.redact_arg(value) == scan_mcp.REDACTED


def test_secret_in_url_path_and_json_arg_is_redacted():
    hook = "9z9z" + "Kq4WmX2" + "Lp7Rt5Vb8Nc3"
    url = "https://hooks.slack.com/services/T0AAAA/B0BBBB/" + hook
    out = scan_mcp.redact_arg(url)
    assert hook not in out
    assert out.startswith("https://hooks.slack.com/services/")
    key = "60ff5a50" + "1c2d3e4f" + "5a6b7c8d" + "9e0f4eb4"
    cfg = json.dumps({"apiKey": key, "region": "us", "nested": {"token": "short"}})
    cleaned = scan_mcp.redact_arg(cfg)
    assert key not in cleaned and "short" not in cleaned
    assert json.loads(cleaned)["region"] == "us"
    assert scan_mcp.redact_arg('{"broken": ') == scan_mcp.REDACTED


def test_same_product_two_scopes_qualified_selector_is_refused(
    tmp_path, capsys, fake_scanner
):
    sysroot = tmp_path / "sysroot"
    _write(
        sysroot / "etc" / "gemini-cli" / "settings.json",
        {"mcpServers": {"fs": {"command": "npx", "args": ["-y", "good-pkg"]}}},
    )
    _gemini(tmp_path, {"fs": {"command": "npx", "args": ["-y", "evil-pkg"]}})
    tools = _tools_file(tmp_path)
    code = main(
        [
            "scan-mcp",
            "--home",
            str(tmp_path),
            "--system-root",
            str(sysroot),
            "--platform",
            "linux",
            "--tools-json",
            f"gemini_cli:fs={tools}",
        ]
    )
    assert code == 2
    assert not fake_scanner.exists()
    capsys.readouterr()
    _, out = _run(
        tmp_path, "--tools-json", f"gemini_cli:user:fs={tools}", capsys=capsys
    )
    by_scope = {r["scope"]: r["body"] for r in out["items"]}
    assert "report" in by_scope["user"]
    assert by_scope["managed"]["unscanned_reason"] == "analyzer_disabled"


def test_tools_over_receiver_limit_fail_before_scanning(tmp_path, capsys, fake_scanner):
    _gemini(tmp_path, {"s": {"command": "npx", "args": ["-y", "pkg"]}})
    names = [f"t{i}" for i in range(scan_mcp.MAX_TOOLS + 1)]
    path = _tools_file(tmp_path, names)
    code = main(
        [
            "scan-mcp",
            "--home",
            str(tmp_path),
            "--platform",
            "linux",
            "--tools-json",
            f"s={path}",
        ]
    )
    assert code == 2
    assert "上限" in capsys.readouterr().err
    assert not fake_scanner.exists()
    # 上限ちょうどは通る。
    _, out = _run(
        tmp_path,
        "--tools-json",
        f"s={_tools_file(tmp_path, names[:-1])}",
        capsys=capsys,
    )
    assert len(out["items"][0]["body"]["tools"]) == scan_mcp.MAX_TOOLS


def test_tool_definition_over_byte_limit_fails(tmp_path):
    big = {
        "tools": [
            {"name": "t", "description": "x" * scan_mcp.MAX_TOOL_DEFINITION_BYTES}
        ]
    }
    with pytest.raises(ValueError, match="バイト"):
        scan_mcp.load_tools(_write(tmp_path / "big.json", big))


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--yes", "--prefix", "/x", "pkg@1.0.0"], ("pkg", "1.0.0")),
        (["-w", "ws", "pkg"], ("pkg", "")),
        (["--unknown-flag", "value", "pkg"], ("", "")),
    ],
)
def test_npx_value_flags_are_not_taken_as_package(args, expected):
    assert scan_mcp.package_of("npx", args) == expected


def test_uvx_unknown_flag_is_not_guessed():
    assert scan_mcp.package_of("uvx", ["--refresh-package", "other", "tool==1"]) == (
        "tool",
        "1",
    )
    assert scan_mcp.package_of("uvx", ["--mystery", "x", "tool"]) == ("", "")


def test_receiver_length_limits_are_checked_before_sending(
    tmp_path, capsys, no_scanner
):
    _gemini(
        tmp_path,
        {
            "n" * (scan_mcp.MAX_NAME_LEN + 1): {"command": "npx", "args": ["x"]},
            "v": {
                "command": "npx",
                "args": ["pkg@" + "1" * (scan_mcp.MAX_VERSION_LEN + 1)],
            },
            "u": {"url": "https://a.example/" + "p" * scan_mcp.MAX_URL_LEN},
        },
    )
    code, out = _run(tmp_path, capsys=capsys)
    assert code == 1
    assert [("body" in r, bool(r.get("error"))) for r in out["items"]] == [
        (False, True)
    ] * 3


def test_scanner_output_over_cap_is_unscanned(
    tmp_path, capsys, fake_scanner, monkeypatch
):
    monkeypatch.setattr(scan_mcp, "MAX_SCANNER_OUTPUT", 64)
    _gemini(tmp_path, {"s": {"command": "npx", "args": ["-y", "pkg"]}})
    _, out = _run(tmp_path, "--tools-json", f"s={_tools_file(tmp_path)}", capsys=capsys)
    assert out["items"][0]["body"]["unscanned_reason"] == "analyzer_disabled"
    assert "大きすぎる" in out["items"][0]["scan_note"]


def test_scanner_output_over_cap_is_stopped_while_running(
    tmp_path, capsys, fake_scanner, monkeypatch
):
    # 上限を超えて書き続ける走査器は、終わるのを待たずに止める。待つと時間切れになる。
    monkeypatch.setattr(scan_mcp, "MAX_SCANNER_OUTPUT", 64)
    monkeypatch.setenv("FAKE_SCANNER_MODE", "spew")
    _gemini(tmp_path, {"s": {"command": "npx", "args": ["-y", "pkg"]}})
    _, out = _run(
        tmp_path,
        "--timeout",
        "3",
        "--tools-json",
        f"s={_tools_file(tmp_path)}",
        capsys=capsys,
    )
    assert out["items"][0]["body"]["unscanned_reason"] == "analyzer_disabled"
    assert "大きすぎる" in out["items"][0]["scan_note"]


def test_script_hash_launch_shapes(tmp_path):
    script = tmp_path / "server.py"
    script.write_bytes(b"print(1)")
    expected = hashlib.sha256(b"print(1)").hexdigest()
    assert (
        scan_mcp.artifact_hash_of("python", [str(script), "-m", "x"], None) == expected
    )
    assert scan_mcp.artifact_hash_of("python", ["-u", str(script)], None) == expected
    assert scan_mcp.artifact_hash_of("python", ["-m", "mod", str(script)], None) is None
    # -m の値がそのままファイルの名前でも、モジュールとして実行するためハッシュを取らない。
    assert (
        scan_mcp.artifact_hash_of("python", ["-m", "server.py"], str(tmp_path)) is None
    )
    assert (
        scan_mcp.artifact_hash_of("C:\\Program Files\\node.exe", [str(script)], None)
        == expected
    )
    assert (
        scan_mcp.artifact_hash_of(
            "uv", ["--directory", str(tmp_path), "run", "server.py"], None
        )
        == expected
    )
    assert (
        scan_mcp.artifact_hash_of(
            "uv", ["run", "--directory", str(tmp_path), "server.py"], None
        )
        == expected
    )
    assert scan_mcp.artifact_hash_of("uv", ["run", "server.py"], None) is None
    assert scan_mcp.package_of("npx.cmd", ["-y", "pkg@2.0.0"]) == ("pkg", "2.0.0")


# --- 製品ごとの置き場所 -------------------------------------------------------------------


def _read(home: Path, *, cwd: Path | None = None, platform: str = "linux"):
    return read_configured_servers(
        platform=platform, home=home, system_root=home / "sys", cwd=cwd or home / "work"
    )


SAFE_STDIO = {
    "command": "npx",
    "args": ["-y", "safe-pkg@1.0.0"],
    "env": {"API_TOKEN": ENV_SECRET},
}
EVIL_STDIO = {
    "command": "npx",
    "args": ["-y", "evil-pkg@6.6.6", "--token", FLAG_SECRET],
}


def _items(out: dict) -> list[tuple[str, str, str]]:
    return [
        (
            r["product"],
            r["scope"],
            r["body"]["artifact"].get("package") or r["body"]["artifact"].get("url"),
        )
        for r in out["items"]
    ]


def _assert_no_secrets(out: dict) -> None:
    text = json.dumps(out)
    for secret in (ENV_SECRET, FLAG_SECRET, HEADER_SECRET):
        assert secret not in text


def test_claude_code_user_projects_and_cwd(tmp_path, capsys, no_scanner, _isolated_cwd):
    _write(
        tmp_path / ".claude.json",
        {
            "mcpServers": {"fs": SAFE_STDIO},
            "projects": {
                "/p/one": {"mcpServers": {"fs": EVIL_STDIO}},
                "/p/two": {
                    "mcpServers": {
                        "api": {
                            "type": "http",
                            "url": "https://a.example/mcp",
                            "headers": {"Authorization": HEADER_SECRET},
                        }
                    }
                },
                "/p/none": {"allowedTools": []},
            },
        },
    )
    _write(
        _isolated_cwd / ".mcp.json",
        {"mcpServers": {"shared": {"type": "sse", "url": "https://s.example/sse"}}},
    )
    # 親のディレクトリの .mcp.json はたどらない。
    _write(_isolated_cwd.parent / ".mcp.json", {"mcpServers": {"parent": SAFE_STDIO}})
    code, out = _run(tmp_path, capsys=capsys)
    claude = [i for i in _items(out) if i[0] == "claude_code"]
    assert claude == [
        ("claude_code", "user", "safe-pkg"),
        ("claude_code", "project:/p/one", "evil-pkg"),
        ("claude_code", "project:/p/two", "https://a.example/mcp"),
        ("claude_code", "cwd", "https://s.example/sse"),
    ]
    _assert_no_secrets(out)
    assert code == 0


def test_claude_desktop_darwin_only(tmp_path):
    rel = Path("Library/Application Support/Claude/claude_desktop_config.json")
    _write(tmp_path / rel, {"mcpServers": {"fs": SAFE_STDIO}})
    mac = _read(tmp_path, platform="darwin")
    assert [(s.product, s.name) for s in mac.servers] == [("claude_desktop", "fs")]
    linux = _read(tmp_path)
    assert not [s for s in linux.servers if s.product == "claude_desktop"]
    assert "この OS の置き場所" in linux.refused_products["claude_desktop"]
    # darwin で読めたときは、断る理由を載せない。
    assert "claude_desktop" not in mac.refused_products
    assert mac.unreadable_products == {}


def test_cursor_home_and_cwd_same_name_different_entity(
    tmp_path, capsys, no_scanner, _isolated_cwd
):
    _write(tmp_path / ".cursor" / "mcp.json", {"mcpServers": {"fs": SAFE_STDIO}})
    _write(
        _isolated_cwd / ".cursor" / "mcp.json",
        {
            "mcpServers": {
                "fs": EVIL_STDIO,
                "r": {
                    "url": "https://r.example/mcp",
                    "headers": {"API_KEY": HEADER_SECRET},
                },
            }
        },
    )
    _, out = _run(tmp_path, capsys=capsys)
    assert [i for i in _items(out) if i[0] == "cursor"] == [
        ("cursor", "user", "safe-pkg"),
        ("cursor", "cwd", "evil-pkg"),
        ("cursor", "cwd", "https://r.example/mcp"),
    ]
    _assert_no_secrets(out)


def test_cwd_equal_to_home_is_read_once(tmp_path):
    _write(tmp_path / ".cursor" / "mcp.json", {"mcpServers": {"fs": SAFE_STDIO}})
    read = _read(tmp_path, cwd=tmp_path)
    assert [(s.product, s.scope) for s in read.servers] == [("cursor", "user")]


def test_windsurf_both_documented_files(tmp_path, capsys, no_scanner):
    _write(
        tmp_path / ".config" / "devin" / "mcp_config.json",
        {
            "mcpServers": {
                "fs": SAFE_STDIO,
                "r": {
                    "serverUrl": "https://w.example/mcp",
                    "headers": {"Authorization": HEADER_SECRET},
                },
            }
        },
    )
    _write(
        tmp_path / ".codeium" / "windsurf" / "mcp_config.json",
        {"mcpServers": {"fs": EVIL_STDIO}},
    )
    _, out = _run(tmp_path, capsys=capsys)
    assert [i for i in _items(out) if i[0] == "windsurf"] == [
        ("windsurf", "user", "safe-pkg"),
        ("windsurf", "user", "https://w.example/mcp"),
        ("windsurf", "user", "evil-pkg"),
    ]
    _assert_no_secrets(out)


def test_windsurf_two_url_fields_are_not_guessed(tmp_path, capsys, no_scanner):
    _write(
        tmp_path / ".config" / "devin" / "mcp_config.json",
        {
            "mcpServers": {
                "r": {"serverUrl": "https://a.example/", "url": "https://b.example/"}
            }
        },
    )
    code, out = _run(tmp_path, capsys=capsys)
    assert code == 1 and out["items"][0]["error"]


def test_vscode_workspace_servers_and_jsonc_is_reported(
    tmp_path, capsys, no_scanner, _isolated_cwd
):
    _write(
        _isolated_cwd / ".vscode" / "mcp.json",
        {
            "servers": {
                "fs": {"type": "stdio", **SAFE_STDIO},
                "fs2": {"type": "stdio", **EVIL_STDIO},
                "h": {
                    "type": "http",
                    "url": "https://v.example/mcp",
                    "headers": {"Authorization": HEADER_SECRET},
                },
            }
        },
    )
    _, out = _run(tmp_path, capsys=capsys)
    assert [i for i in _items(out) if i[0] == "vscode_copilot"] == [
        ("vscode_copilot", "cwd", "safe-pkg"),
        ("vscode_copilot", "cwd", "evil-pkg"),
        ("vscode_copilot", "cwd", "https://v.example/mcp"),
    ]
    _assert_no_secrets(out)
    assert "プロファイル" in out["refused_products"]["vscode_copilot"]
    assert "vscode_copilot" not in out["unreadable_products"]
    (_isolated_cwd / ".vscode" / "mcp.json").write_text(
        '{\n // note\n "servers": {}\n}', encoding="utf-8"
    )
    read = _read(tmp_path, cwd=_isolated_cwd)
    assert "JSON として読めない" in read.unreadable_products["vscode_copilot"]


def test_cline_cli_settings(tmp_path, capsys, no_scanner):
    _write(
        tmp_path / ".cline" / "data" / "settings" / "cline_mcp_settings.json",
        {
            "mcpServers": {
                "fs": SAFE_STDIO,
                "fs2": EVIL_STDIO,
                "r": {
                    "type": "streamableHttp",
                    "url": "https://c.example/mcp",
                    "headers": {"Authorization": HEADER_SECRET},
                },
                "legacy": {"url": "https://l.example/sse"},
            }
        },
    )
    _, out = _run(tmp_path, capsys=capsys)
    assert [i[2] for i in _items(out) if i[0] == "cline"] == [
        "safe-pkg",
        "evil-pkg",
        "https://c.example/mcp",
        "https://l.example/sse",
    ]
    _assert_no_secrets(out)


def test_codex_toml(tmp_path, capsys, no_scanner):
    pytest.importorskip("tomllib")
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        'model = "x"\n'
        '[mcp_servers.fs]\ncommand = "npx"\nargs = ["-y", "safe-pkg@1.0.0"]\n'
        f'[mcp_servers.fs.env]\nAPI_TOKEN = "{ENV_SECRET}"\n'
        f'[mcp_servers.evil]\ncommand = "npx"\nargs = ["-y", "evil-pkg@6.6.6", "--token", "{FLAG_SECRET}"]\n'
        f'[mcp_servers.r]\nurl = "https://x.example/mcp"\nhttp_headers = {{ Authorization = "{HEADER_SECRET}" }}\n',
        encoding="utf-8",
    )
    _, out = _run(tmp_path, capsys=capsys)
    rows = [r for r in out["items"] if r["product"] == "codex"]
    assert [i[2] for i in _items(out) if i[0] == "codex"] == [
        "safe-pkg",
        "evil-pkg",
        "https://x.example/mcp",
    ]
    assert rows[2]["header_keys"] == ["Authorization"]
    _assert_no_secrets(out)
    path.write_text("[mcp_servers.fs\n", encoding="utf-8")
    assert "TOML" in _read(tmp_path).unreadable_products["codex"]


def test_scan_count_equals_read_count_across_products(
    tmp_path, capsys, no_scanner, _isolated_cwd
):
    _write(tmp_path / ".claude.json", {"mcpServers": {"a": SAFE_STDIO}})
    _write(tmp_path / ".cursor" / "mcp.json", {"mcpServers": {"a": EVIL_STDIO}})
    _write(_isolated_cwd / ".vscode" / "mcp.json", {"servers": {"a": SAFE_STDIO}})
    read = _read(tmp_path, cwd=_isolated_cwd)
    _, out = _run(tmp_path, capsys=capsys)
    assert out["servers_read"] == len(read.servers) == len(out["items"]) == 3


# --- 監査の指摘 (読み取りの形と秘密) への対応 ---------------------------------------------

DB_PASS = "pw" + "Secret" + "8812"


@pytest.mark.parametrize(
    "args",
    [
        [f"DATABASE_URL=postgres://u:{DB_PASS}@h/db"],
        [f"--db=postgres://u:{DB_PASS}@h/db"],
        [f"database_url='postgres://u:{DB_PASS}@h/db'"],
        [f"Db: postgres://u:{DB_PASS}@h/db"],
        ["--connection-string", f"Server=x;Password={DB_PASS}"],
        ["--connection-string", f"Server=x; password = {DB_PASS}"],
        ["-c", f"node s.js --api-key {DB_PASS}"],
        ["-c", f'node s.js --API-KEY "{DB_PASS}"'],
        [f"postgres://h/db?sslmode=require&password={DB_PASS}"],
        [f"--opts=a postgres://u:{DB_PASS}@h/db"],
    ],
)
def test_connection_strings_and_embedded_commands_hide_passwords(
    tmp_path, capsys, no_scanner, args
):
    _gemini(tmp_path, {"s": {"command": "srv", "args": args}})
    _, out = _run(tmp_path, "--dry-run", capsys=capsys)
    assert DB_PASS not in json.dumps(out)
    assert "body" in out["items"][0]


def test_assigned_url_keeps_host_and_path(tmp_path):
    out = scan_mcp.redact_arg(f"DATABASE_URL=postgres://u:{DB_PASS}@db.example/app")
    assert out == "DATABASE_URL=postgres://<redacted>@db.example/app"
    out = scan_mcp.redact_arg(f"API_URL=https://h.example/x?sig={DB_PASS}")
    assert out == "API_URL=https://h.example/x?<redacted>"


def test_remote_url_with_secret_path_is_not_sent(tmp_path, capsys, no_scanner):
    path_key = "sk-" + "ak-" + "Zx9Qw8Er7Ty6Ui5Op4"
    hex_key = "60ff5a50" + "1c2d3e4f" + "5a6b7c8d" + "9e0f4eb4"
    _gemini(
        tmp_path,
        {
            "z": {"httpUrl": f"https://actions.zapier.com/mcp/{path_key}/sse"},
            "h": {"url": f"https://mcp.example.com/{hex_key}"},
            "ok": {"url": "https://ok.example.com/mcp"},
        },
    )
    code, out = _run(tmp_path, capsys=capsys)
    text = json.dumps(out)
    assert path_key not in text and hex_key not in text
    by_name = {r["server"]: r for r in out["items"]}
    assert "body" not in by_name["z"] and "秘密" in by_name["z"]["error"]
    assert "body" not in by_name["h"]
    assert by_name["ok"]["body"]["artifact"]["url"] == "https://ok.example.com/mcp"
    assert code == 1


@pytest.mark.parametrize("bad_type", [["http"], {"t": "http"}, 3])
def test_non_string_type_is_one_item_error(tmp_path, capsys, no_scanner, bad_type):
    _gemini(
        tmp_path,
        {
            "bad": {"type": bad_type, "url": "https://b.example/mcp"},
            "good": {"command": "npx", "args": ["-y", "pkg"]},
        },
    )
    code, out = _run(tmp_path, capsys=capsys)
    by_name = {r["server"]: r for r in out["items"]}
    assert by_name["bad"]["error"] == "type が文字列でない"
    assert by_name["good"]["body"]["artifact"]["package"] == "pkg"
    assert code == 1


def test_unexpected_error_is_isolated_per_item(
    tmp_path, capsys, no_scanner, monkeypatch
):
    real = scan_mcp.build_artifact

    def flaky(server):
        if server.name == "boom":
            raise KeyError(ENV_SECRET)
        return real(server)

    monkeypatch.setattr(scan_mcp, "build_artifact", flaky)
    _gemini(
        tmp_path,
        {"boom": {"command": "x"}, "good": {"command": "npx", "args": ["pkg"]}},
    )
    code, out = _run(tmp_path, capsys=capsys)
    by_name = {r["server"]: r for r in out["items"]}
    assert by_name["boom"]["error"] == "定義を処理できない: KeyError"
    assert ENV_SECRET not in json.dumps(out)
    assert "body" in by_name["good"]
    assert code == 1


def test_fifo_config_does_not_block(tmp_path, _isolated_cwd):
    import subprocess as sp
    import sys

    os.mkfifo(_isolated_cwd / ".mcp.json")
    _write(tmp_path / ".cursor" / "mcp.json", {"mcpServers": {"a": SAFE_STDIO}})
    code = (
        "import json,sys;from pathlib import Path;"
        "from senda_argus_hooks.cli.mcp_configs import read_configured_servers as r;"
        "x=r(platform='linux',home=Path(sys.argv[1]),system_root=Path(sys.argv[1])/'sys',cwd=Path(sys.argv[2]));"
        "print(json.dumps([[s.name for s in x.servers],x.unreadable_products]))"
    )
    proc = sp.run(
        [sys.executable, "-c", code, str(tmp_path), str(_isolated_cwd)],
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    names, unreadable = json.loads(proc.stdout)
    assert names == ["a"]
    assert "通常のファイルでない" in unreadable["claude_code"]


def test_symlink_loop_is_reported_not_raised(tmp_path, _isolated_cwd):
    loop = _isolated_cwd / ".mcp.json"
    loop.symlink_to(loop)
    _write(tmp_path / ".cursor" / "mcp.json", {"mcpServers": {"a": SAFE_STDIO}})
    read = _read(tmp_path, cwd=_isolated_cwd)
    assert [s.name for s in read.servers] == ["a"]
    assert "claude_code" in read.unreadable_products


def test_symlink_loop_resolve_error_is_reported(tmp_path, monkeypatch):
    _write(tmp_path / ".cursor" / "mcp.json", {"mcpServers": {"a": SAFE_STDIO}})

    def broken(self, *a, **k):
        raise RuntimeError("loop")

    monkeypatch.setattr(Path, "resolve", broken)
    read = _read(tmp_path)
    assert read.servers == []
    assert "解決できない" in read.unreadable_products["cursor"]


@pytest.mark.parametrize(
    ("rel", "data", "product"),
    [
        (".cursor/mcp.json", [], "cursor"),
        (".cursor/mcp.json", "text", "cursor"),
        (".claude.json", {"projects": {"/p": "x"}}, "claude_code"),
    ],
)
def test_wrong_shapes_are_reported(tmp_path, rel, data, product):
    _write(tmp_path / rel, data)
    read = _read(tmp_path)
    assert "オブジェクトでない" in read.unreadable_products[product]


def test_codex_upper_level_not_object_is_reported(tmp_path):
    pytest.importorskip("tomllib")
    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('mcp_servers = "x"\n', encoding="utf-8")
    assert "オブジェクトでない" in _read(tmp_path).unreadable_products["codex"]


def test_missing_key_is_not_reported(tmp_path):
    _write(tmp_path / ".cursor" / "mcp.json", {"other": 1})
    _write(tmp_path / ".claude.json", {"projects": {"/p": {"allowedTools": []}}})
    read = _read(tmp_path)
    assert read.unreadable_products == {}


def test_read_failure_sets_exit_code(tmp_path, capsys, no_scanner):
    _write(tmp_path / ".cursor" / "mcp.json", {"mcpServers": {"a": SAFE_STDIO}})
    (tmp_path / ".claude.json").write_text("{broken", encoding="utf-8")
    code, out = _run(tmp_path, capsys=capsys)
    assert out["servers_read"] == len(out["items"]) == 1
    assert code == 1
    (tmp_path / ".claude.json").unlink()
    code, _ = _run(tmp_path, capsys=capsys)
    assert code == 0


def test_tools_selector_with_equals_in_project_path(tmp_path, capsys, fake_scanner):
    _write(
        tmp_path / ".claude.json",
        {"projects": {"/w/a=b": {"mcpServers": {"fs": SAFE_STDIO}}}},
    )
    tools = _tools_file(tmp_path)
    _, out = _run(
        tmp_path,
        "--tools-json",
        f"claude_code:project:/w/a=b:fs={tools}",
        capsys=capsys,
    )
    assert "report" in out["items"][0]["body"]
    assert scan_mcp.parse_tools_options(["a=b=c"]) == {"a=b": Path("c")}
