from __future__ import annotations

import os

import ollama

from senda_argus_hooks import flush, register, shutdown


EXPORT_PATH = os.environ.get("ARGUS_EXPORT_PATH", "./logs/ollama-events.jsonl")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "argus-qwen25-14b-toolplan:latest")


register(
    project="ollama-sdk-example",
    environment="local",
    auto_instrument=True,
    exporters=[{"type": "jsonl", "path": EXPORT_PATH}],
    capture_prompt=False,
    capture_response=False,
    redact=True,
)

try:
    client = ollama.Client(host=OLLAMA_BASE_URL)
    response = client.chat(
        model=OLLAMA_MODEL,
        messages=[
            {"role": "system", "content": "You are a defensive security assistant."},
            {"role": "user", "content": "Summarize why AI agent tool-call logging is important in one sentence."},
        ],
    )
    print(response["message"]["content"])
finally:
    flush()
    shutdown()
    print(f"Log path: {EXPORT_PATH}")
