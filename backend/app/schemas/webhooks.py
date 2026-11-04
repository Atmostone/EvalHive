"""Pydantic schemas for the agent → orchestrator webhook protocol."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, Field  # noqa: F401

SCHEMA_VERSION = "1.0"


class TokenUsage(BaseModel):
    """Accepts both schema names (input/output and input_tokens/output_tokens).

    Every field an agent reports has to be DECLARED here — `extra: "ignore"`
    means an undeclared key is dropped between the wire and the database with no
    error anywhere (SPA-114 was caught exactly that way, on its first live run).
    """

    input_tokens: int = Field(default=0, validation_alias=AliasChoices("input_tokens", "input"))
    output_tokens: int = Field(default=0, validation_alias=AliasChoices("output_tokens", "output"))
    # A SUBSET of output_tokens, not an addition — reasoning tokens are billed
    # inside `completion_tokens` (SPA-114). Declared here or `extra: ignore`
    # silently drops it on the way in, which is how the first live run reported
    # 103 reasoning tokens in its webhook and stored a task that had none.
    # None, not 0: a model that does not reason and a provider that does not
    # report the split are both "unknown", and a zero would read as "thought
    # nothing".
    reasoning_tokens: int | None = None

    model_config = {"populate_by_name": True, "extra": "ignore"}


class ProgressData(BaseModel):
    current_step: str | None = None
    tool_name: str | None = None
    iteration: int | None = None
    tokens_used_so_far: TokenUsage | None = None
    recent_output: str | None = None


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

    kind: str | None = Field(default=None, max_length=32)
    exception: str | None = Field(default=None, max_length=128)
    status_code: int | None = None
    message: str | None = Field(default=None, max_length=2000)


class FailedData(BaseModel):
    error: str = ""
    # Declared, or Pydantic's extra="ignore" would drop it silently — which is how
    # a failed run's `result_summary` has been vanishing since SPA-40.
    failure: FailureFacts | None = None
    token_usage: TokenUsage = TokenUsage()


class AbortedData(BaseModel):
    reason: str = ""
    token_usage: TokenUsage = TokenUsage()


class _ProgressEvent(BaseModel):
    schema_version: str = SCHEMA_VERSION
    event: Literal["progress"]
    timestamp: datetime | None = None
    task_id: str | None = None
    idempotency_key: str | None = None
    data: ProgressData


class _CompletedEvent(BaseModel):
    schema_version: str = SCHEMA_VERSION
    event: Literal["completed"]
    timestamp: datetime | None = None
    task_id: str | None = None
    idempotency_key: str | None = None
    data: CompletedData


class _FailedEvent(BaseModel):
    schema_version: str = SCHEMA_VERSION
    event: Literal["failed"]
    timestamp: datetime | None = None
    task_id: str | None = None
    idempotency_key: str | None = None
    data: FailedData


class _AbortedEvent(BaseModel):
    schema_version: str = SCHEMA_VERSION
    event: Literal["aborted"]
    timestamp: datetime | None = None
    task_id: str | None = None
    idempotency_key: str | None = None
    data: AbortedData


AgentWebhookEvent = Annotated[
    _ProgressEvent | _CompletedEvent | _FailedEvent | _AbortedEvent,
    Field(discriminator="event"),
]
