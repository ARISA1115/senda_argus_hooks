from __future__ import annotations

from openai import OpenAI

from senda_argus_hooks import register, shutdown


def main() -> None:
    register(
        project="openai-example",
        exporters=[{"type": "jsonl", "path": "./logs/openai-events.jsonl"}],
        auto_instrument=True,
    )

    try:
        # Requires the openai package and OPENAI_API_KEY.
        client = OpenAI()
        client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Write a haiku."}],
        )
    finally:
        shutdown()


if __name__ == "__main__":
    main()
