from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    database_url: str = "postgresql+asyncpg://evalhive:password@postgres:5432/evalhive"
    qdrant_url: str = "http://qdrant:6333"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"

    # Optional bootstrap LLM env vars. If all three are set, an initial
    # Provider+Model row is seeded into the default workspace on first boot
    # (see app.main.seed_default_provider). Otherwise the admin must add
    # a provider via UI → Settings → Providers & Models.
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    # Paths
    data_dir: str = "/data"  # inside api container
    host_data_dir: str = "./data"  # host path for agent container volume mounts
    # Administrator-installed manifests; loading never imports connector code.
    harness_manifest_dir: str = str(Path(__file__).resolve().parents[2] / "harnesses")
    benchmark_manifest_dir: str = str(Path(__file__).resolve().parents[2] / "benchmarks")
    # Product dispatch is enabled only with installed packages and deployment paths.
    benchmark_runtime_enabled: bool = False
    host_benchmark_manifest_dir: str = ""
    benchmark_retained_catalog_roots: dict[str, str] = Field(default_factory=dict)
    benchmark_evidence_bucket: str = "benchmark-evidence"
    benchmark_group_memory_mb: int = Field(default=4096, gt=0)
    benchmark_group_cpu_cores: float = Field(default=2.0, gt=0, allow_inf_nan=False)
    benchmark_stop_grace_seconds: float = Field(default=10, ge=0.01, le=60, allow_inf_nan=False)
    benchmark_resource_finalizers: bool = True
    benchmark_linux_capabilities: list[str] = [
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "SETGID",
        "SETUID",
    ]
    runtime_network: str = "evalhive_evalhive-net"
    runtime_max_cpu_quota: int = 200000
    runtime_max_memory_mb: int = 2048
    # Server-owned task environments may expose additional dependency trees read-only.
    runtime_readonly_roots: list[str] = []
    runtime_max_concurrency: int = 1
    runner_api_url: str = "http://api:8000"
    # Absolute HOST path of the toolathlon_gym clone, used by the Experiment
    # Runner to bind-mount the gym into preprocess/eval containers (docker-py
    # talks to the host daemon). Empty unless running Toolathlon experiments.
    toolathlon_gym_path: str = ""

    # Auth
    jwt_secret: str = ""  # MUST be set in .env for production
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 60 * 24  # 24h

    # Deployment (SPA-78). All default to current dev behavior; prod sets them in .env.
    # CORS: comma-separated allowed origins; empty → the dev localhost list (app.main).
    cors_allowed_origins: str = ""
    # Public demos close self-service signup (the demo account is provisioned out of band).
    allow_open_registration: bool = True
    # When true, boot refuses an empty or the public dev-placeholder JWT_SECRET.
    # Off by default so local dev (which ships the placeholder) is unaffected.
    require_strong_secrets: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
