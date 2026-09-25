# Senda-Argus Python Zero-code Deployment

既存AI Agentのソースコードを変更せず、Python起動時にSenda-Argus Hookを自動ロードする導入方式です。

## 目的

通常のSDK導入ではAgent側に`register()`等の変更が必要ですが、本方式では管理者が一度インストーラを実行します。
以後、対象Python/venvで起動する既存Agentは`.pth`からSenda-Argusを自動ロードします。

```text
Existing Agent
    |
    | python agent.py   (Agent source unchanged)
    v
Python startup
    |
    +-- senda_argus_autohook.pth
            |
            +-- senda_argus_hooks.autohook.bootstrap()
                    |
                    +-- OpenAI / Anthropic / LiteLLM / Ollama
                    +-- MCP Python / OpenAI Agents / Argus SDK
```

## 配布物

- `tools/senda_argus_zero_install.py` : 初回導入・検出・解除用スタンドアロン管理スクリプト
- `dist/senda_argus_hooks-0.8.0-py3-none-any.whl` : オフライン導入可能なSDK wheel
- `python/` : SDKソース

対象Pythonは3.10以上です。

## 1. 対象Python/Agentを検出

```bash
python3 tools/senda_argus_zero_install.py scan
```

検出元:

- インストーラ自身のPython
- `PATH`上のPython
- Linux `/proc`上で稼働中のPythonプロセス
- `VIRTUAL_ENV`
- `/opt`, `/srv`, `/app`, `/var/www`, `/home`配下の`pyvenv.cfg`

範囲を限定する場合:

```bash
python3 tools/senda_argus_zero_install.py scan \
  --scan-root /opt/my-agent \
  --scan-root /srv/agents
```

## 2. 1つの既存venvへ導入

```bash
sudo python3 tools/senda_argus_zero_install.py install \
  --python /opt/my-agent/.venv/bin/python \
  --endpoint https://argus.example.local \
  --project my-agent \
  --environment prod \
  --exporters argus
```

APIキーはコマンドラインへ直接書かず、ファイル利用を推奨します。

```bash
sudo install -m 600 /dev/null /root/argus-api-key
sudo sh -c 'printf "%s" "YOUR_KEY" > /root/argus-api-key'

sudo python3 tools/senda_argus_zero_install.py install \
  --python /opt/my-agent/.venv/bin/python \
  --endpoint https://argus.example.local \
  --api-key-file /root/argus-api-key \
  --project my-agent \
  --environment prod \
  --exporters argus
```

## 3. 検出した全Python環境へ一括導入

`--all`は意図しない全環境変更を防ぐため`--yes`が必須です。

```bash
sudo python3 tools/senda_argus_zero_install.py install \
  --all --yes \
  --scan-root /opt \
  --scan-root /srv \
  --endpoint https://argus.example.local \
  --api-key-file /root/argus-api-key \
  --project production-agents \
  --environment prod \
  --exporters argus
```

`pip`が存在しないvenvに`ensurepip`を許可する場合のみ`--bootstrap-pip`を付与します。

## 設定ファイル

rootで導入すると既定では次に作成します。

```text
/etc/senda-argus/hooks.env
```

一般ユーザーでは:

```text
~/.config/senda-argus/hooks.env
```

HookはPython起動時にこの設定を自動読込するため、既存Agentのsystemd unitや起動スクリプトへ環境変数を追加する必要はありません。
プロセス側ですでに設定されている`SENDA_ARGUS_*`環境変数は設定ファイルより優先されます。

独自パスは:

```bash
--config /opt/senda/hooks.env
```

または実行環境で:

```bash
export SENDA_ARGUS_CONFIG=/opt/senda/hooks.env
```

## 状態確認

```bash
python3 tools/senda_argus_zero_install.py status \
  --python /opt/my-agent/.venv/bin/python
```

`package_installed=true`, `hook_installed=true`, `hook_managed=true`を確認します。

## 適用タイミング

`.pth`はPythonインタプリタ起動時にロードされます。
したがって、インストール時点ですでに稼働中のAgentは再起動が必要です。
インストーラは検出できた稼働PIDを`restart_required_pids`として表示します。

## 一時無効化

Agentソースを変更せず環境変数だけで停止できます。

```bash
export SENDA_ARGUS_ENABLED=false
```

または`hooks.env`を変更します。

## アンインストール

Hookだけ解除:

```bash
sudo python3 tools/senda_argus_zero_install.py uninstall \
  --python /opt/my-agent/.venv/bin/python
```

SDKも削除:

```bash
sudo python3 tools/senda_argus_zero_install.py uninstall \
  --python /opt/my-agent/.venv/bin/python \
  --remove-sdk
```

全対象から解除する場合は`--all --yes`が必要です。

## Fail-open

Senda-Argus初期化や送信で問題が発生してもAgentを停止させない設計です。
`SENDA_ARGUS_BOOTSTRAP_DEBUG=true`の場合のみbootstrapエラーをstderrへ表示します。

## セキュリティ上の既定値

本文データは既定で取得しません。

```text
SENDA_ARGUS_CAPTURE_PROMPT=false
SENDA_ARGUS_CAPTURE_RESPONSE=false
SENDA_ARGUS_CAPTURE_ARGUMENTS=false
SENDA_ARGUS_CAPTURE_RESULT=false
SENDA_ARGUS_CAPTURE_HASH=true
SENDA_ARGUS_REDACT=true
```

`hooks.env`は可能な限り`0600`で作成します。
