"""EvalHive connector contract. No backend, Docker or LLM dependencies."""

from .protocol import (
    PROTOCOL_VERSION,
    ProtocolError,
    command,
    decode_line,
    encode_line,
    error_response,
    response,
    validate,
    validate_run,
)
from .resources import resource_config, resources

__all__ = [
    "PROTOCOL_VERSION",
    "ProtocolError",
    "command",
    "decode_line",
    "encode_line",
    "error_response",
    "response",
    "validate",
    "validate_run",
    "resource_config",
    "resources",
]
