"""gigaforge.client - hardened GigaChat transport for IGAFORGE.

Public surface (nothing else should be imported by other modules)::

    from gigaforge.client import GigaClient, ClientConfig

    client = GigaClient(ClientConfig.from_env())
    print(client.complete([{"role": "user", "content": "hi"}]).content)

No LangChain, no LiteLLM, no OpenAI SDK: this package talks to the REST/SSE API
directly over an injectable transport.
"""
from __future__ import annotations

from .auth import Token, TokenProvider, get_token_provider, reset_default_providers
from .client import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE, GigaClient, RequestPlan
from .config import ClientConfig
from .errors import (
    AuthError,
    ConfigurationError,
    GigaForgeError,
    NetworkError,
    RateLimitError,
    RequestError,
    RequestTimeout,
    ServerError,
    StreamProtocolError,
    ToolCallError,
    error_for_exception,
    error_for_status,
)
from .functions import (
    build_tool_schema,
    detect_text_function_call,
    finalize_function_call,
    function_names,
    normalize_functions,
    parse_function_call,
)
from .metrics import CompositeMetrics, InMemoryMetrics, MetricsSink, NullMetrics
from .ratelimit import RateLimiter, Reservation, TokenBucket
from .retry import RetryPolicy, parse_retry_after, retry_call
from .sse import DONE_SENTINEL, SSEEvent, SSEParser
from .streaming import StreamAggregator, StreamStats
from .transport import RequestsTransport, Response, Transport
from .types import (
    ChatMessage,
    ChatResult,
    FunctionCall,
    FunctionSpec,
    ModelInfo,
    StreamDelta,
    Usage,
    coerce_messages,
    estimate_token_count,
    join_message_text,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # facade
    "GigaClient",
    "ClientConfig",
    "RequestPlan",
    # auth
    "Token",
    "TokenProvider",
    "get_token_provider",
    "reset_default_providers",
    # transport
    "Transport",
    "RequestsTransport",
    "Response",
    # streaming
    "SSEParser",
    "SSEEvent",
    "DONE_SENTINEL",
    "StreamAggregator",
    "StreamStats",
    # functions
    "FunctionSpec",
    "FunctionCall",
    "parse_function_call",
    "detect_text_function_call",
    "normalize_functions",
    "function_names",
    "finalize_function_call",
    "build_tool_schema",
    # reliability
    "RetryPolicy",
    "retry_call",
    "parse_retry_after",
    "RateLimiter",
    "Reservation",
    "TokenBucket",
    # observability
    "MetricsSink",
    "InMemoryMetrics",
    "NullMetrics",
    "CompositeMetrics",
    # types
    "ChatMessage",
    "ChatResult",
    "StreamDelta",
    "Usage",
    "ModelInfo",
    "coerce_messages",
    "estimate_token_count",
    "join_message_text",
    # errors
    "GigaForgeError",
    "ConfigurationError",
    "AuthError",
    "RateLimitError",
    "RequestError",
    "ServerError",
    "NetworkError",
    "RequestTimeout",
    "StreamProtocolError",
    "ToolCallError",
    "error_for_status",
    "error_for_exception",
    # tuning defaults
    "DEFAULT_TEMPERATURE",
    "DEFAULT_MAX_TOKENS",
]
