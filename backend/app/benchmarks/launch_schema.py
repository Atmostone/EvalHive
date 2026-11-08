"""Shared bounded product limits for individual and matrix benchmark launches."""

from pydantic import BaseModel, ConfigDict, Field


class TrialLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wall_time_seconds: int = Field(default=1800, ge=1, le=86400)
    memory_mb: int = Field(default=4096, ge=1, le=1048576)
    cpu_cores: float = Field(default=2.0, gt=0, le=1024, allow_inf_nan=False)
    disk_mb: int = Field(default=4096, ge=1, le=2147483647)
    model_requests: int = Field(default=100, ge=0, le=1000000)
