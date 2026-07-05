from senda_argus_hooks.integrations.langflow import register_langflow_hooks


# Example for a simple Langflow Python/custom component.
# Run this once near the beginning of the workflow.
result = register_langflow_hooks(
    project="langflow-agent",
    environment="dev",
    export_path="./langflow-events.jsonl",
    auto_instrument=True,
    capture_prompt=False,
    capture_response=False,
    capture_arguments=True,
    capture_result=False,
    redact=True,
)

print(result)
