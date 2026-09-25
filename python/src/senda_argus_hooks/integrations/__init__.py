"""Optional framework integrations for agent/runtime observability."""

from .langchain import SendaArgusCallbackHandler
from .langflow import langflow_callback_handler, register_langflow_hooks
from .langgraph import astream_with_argus, stream_with_argus
from .llamaindex import (
    RAGInstrumentation,
    SendaArgusLlamaIndexCallbackHandler,
    aretrieve_with_argus,
    embed_text_with_argus,
    embed_texts_with_argus,
    instrument_rag,
    query_with_argus,
    retrieve_with_argus,
)
from .openai_agents import OpenAIAgentsInstrumentor, SendaArgusOpenAIAgentsProcessor

__all__ = [
    "OpenAIAgentsInstrumentor",
    "RAGInstrumentation",
    "SendaArgusCallbackHandler",
    "SendaArgusLlamaIndexCallbackHandler",
    "SendaArgusOpenAIAgentsProcessor",
    "aretrieve_with_argus",
    "astream_with_argus",
    "embed_text_with_argus",
    "embed_texts_with_argus",
    "instrument_rag",
    "langflow_callback_handler",
    "query_with_argus",
    "register_langflow_hooks",
    "retrieve_with_argus",
    "stream_with_argus",
]
