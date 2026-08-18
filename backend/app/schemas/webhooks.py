"""Pydantic schemas for the agent → orchestrator webhook protocol."""

from datetime import datetime
from typing import Annotated, Literal, Optional, Union

from pydantic import AliasChoices, BaseModel, Field  # noqa: F401

SCHEMA_VERSION = "1.0"


class TokenUsage(BaseModel):
    """Accepts both schema names (input/output and input_tokens/output_tokens)."""

    input_tokens: int = Field(default=0, validation_alias=AliasChoices("input_tokens", "input"))
    output_tokens: int = Field(default=0, validation_alias=AliasChoices("output_tokens", "output"))

    model_config = {"populate_by_name": True, "extra": "ignore"}


class ProgressData(BaseModel):
    current_step: Optional[str] = None
    tool_name: Optional[str] = None
    iteration: Optional[int] = None
    tokens_used_so_far: Optional[TokenUsage] = None
    recent_output: Optional[str] = None


class CompletedData(BaseModel):
    result_summary: str = ""
    files: list[str] = []
    token_usage: TokenUsage = TokenUsage()


class FailureFacts(BaseModel):
    """What the agent observed at the moment it died (SPA-87).

    Facts, not a verdict: the ``kind`` names the site of the failure (a cap-hit is
    not an exception; a crash carries no HTTP status), and the exception class and
    status are passed through untyped. The backend turns these into a failure type
    — see :mod:`app.utils.failures` — so that judgement exists once rather than in
    both images. Every field is optional: an older agent image sends nothing, which
    the classifier reads as «unclassified», never as «clean»."""

    kind: Optional[str] = Field(default=None, max_length=32)
    exception: Optional[str] = Field(default=None, max_length=128)
    status_code: Optional[int] = None
    message: Optional[str] = Field(default=None, max_length=2000)


class FailedData(BaseModel):
    error: str = ""
    # Declared, or Pydantic's extra="ignore" would drop it silently — which is how
    # a failed run's `result_summary` has been vanishing since SPA-40.
    failure: Optional[FailureFacts] = None
    token_usage: TokenUsage = TokenUsage()


class AbortedData(BaseModel):
    reason: str = ""
    token_usage: TokenUsage = TokenUsage()


class _ProgressEvent(BaseModel):
    schema_version: str = SCHEMA_VERSION
    event: Literal["progress"]
    timestamp: Optional[datetime] = None
    task_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    data: ProgressData


class _CompletedEvent(BaseModel):
    schema_version: str = SCHEMA_VERSION
    event: Literal["completed"]
    timestamp: Optional[datetime] = None
    task_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    data: CompletedData


class _FailedEvent(BaseModel):
    schema_version: str = SCHEMA_VERSION
    event: Literal["failed"]
    timestamp: Optional[datetime] = None
    task_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    data: FailedData


class _AbortedEvent(BaseModel):
    schema_version: str = SCHEMA_VERSION
    event: Literal["aborted"]
    timestamp: Optional[datetime] = None
    task_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    data: AbortedData


AgentWebhookEvent = Annotated[
    Union[_ProgressEvent, _CompletedEvent, _FailedEvent, _AbortedEvent],
    Field(discriminator="event"),
]
