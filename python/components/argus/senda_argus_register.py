from __future__ import annotations

from typing import Any

try:
    from langflow.custom import Component
    from langflow.io import BoolInput, Output, StrInput
except Exception:  # Allows this file to be imported in tests without Langflow.
    Component = object
    BoolInput = Output = StrInput = None

from senda_argus_hooks.integrations.langflow import register_langflow_hooks


class SendaArgusRegisterComponent(Component):
    """Langflow custom component that registers Senda-Argus Hooks.

    Add this component near the beginning of a Langflow flow. It configures
    privacy-safe JSONL audit export for downstream LLM, tool, MCP, and RAG
    activity where hooks/callbacks are available.
    """

    display_name = "Senda Argus Register"
    description = "Register Senda-Argus Hooks for Langflow workflow monitoring."
    icon = "shield"
    name = "senda_argus_register"

    if StrInput is not None:
        inputs = [
            StrInput(name="project", display_name="Project", value="langflow-agent"),
            StrInput(name="environment", display_name="Environment", value="dev"),
            StrInput(name="export_path", display_name="JSONL Export Path", value="./langflow-events.jsonl"),
            BoolInput(name="auto_instrument", display_name="Auto Instrument SDKs", value=True),
            BoolInput(name="capture_prompt", display_name="Capture Prompt", value=False),
            BoolInput(name="capture_response", display_name="Capture Response", value=False),
            BoolInput(name="capture_arguments", display_name="Capture Tool Arguments", value=True),
            BoolInput(name="capture_result", display_name="Capture Tool Results", value=False),
        ]
        outputs = [Output(display_name="Status", name="status", method="register_hooks")]

    def register_hooks(self) -> dict[str, Any]:
        return register_langflow_hooks(
            project=self.project,
            environment=self.environment,
            export_path=self.export_path,
            auto_instrument=self.auto_instrument,
            capture_prompt=self.capture_prompt,
            capture_response=self.capture_response,
            capture_arguments=self.capture_arguments,
            capture_result=self.capture_result,
            redact=True,
        )
