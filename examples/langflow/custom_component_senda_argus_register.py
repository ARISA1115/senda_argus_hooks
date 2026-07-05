from __future__ import annotations

from typing import Any

from lfx.custom import Component
from lfx.io import BoolInput, Output, StrInput

from senda_argus_hooks.integrations.langflow import register_langflow_hooks


class SendaArgusRegisterComponent(Component):
    """Register Senda-Argus Hooks for Langflow workflow monitoring."""

    display_name = "Senda Argus Register"
    description = "Register Senda-Argus Hooks for Langflow workflow monitoring."
    icon = "shield"
    name = "senda_argus_register"

    inputs = [
        StrInput(name="project", display_name="Project", value="langflow-agent"),
        StrInput(name="environment", display_name="Environment", value="dev"),
        StrInput(
            name="export_path",
            display_name="JSONL Export Path",
            value="/tmp/senda-argus-langflow-test/logs/langflow-events.jsonl",
        ),
        BoolInput(name="auto_instrument", display_name="Auto Instrument SDKs", value=True),
        BoolInput(name="capture_prompt", display_name="Capture Prompt", value=False),
        BoolInput(name="capture_response", display_name="Capture Response", value=False),
        BoolInput(name="capture_arguments", display_name="Capture Tool Arguments", value=True),
        BoolInput(name="capture_result", display_name="Capture Tool Results", value=False),
    ]

    outputs = [
        Output(display_name="Status", name="status", method="register_hooks"),
    ]

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
