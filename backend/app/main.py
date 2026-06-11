import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.settings import router as settings_router
from app.api.tasks import router as tasks_router
from app.api.templates import router as templates_router
from app.api.agents import router as agents_router
from app.api.webhooks import router as webhooks_router
from app.api.events import router as events_router, ws_router as events_ws_router
from app.api.chat import router as chat_router
from app.api.knowledge import router as knowledge_router
from app.api.memory import router as memory_router
from app.api.analytics import router as analytics_router
from app.api.scheduled_jobs import router as scheduled_jobs_router
from app.api.agent_logs import router as agent_logs_router, ws_router as agent_logs_ws_router
from app.api.providers import router as providers_router, models_router
from app.api.data_lake import router as data_lake_router
from app.api.quality import router as quality_router
from app.api.registry import router as registry_router
from app.api.experiments import router as experiments_router
from app.api.workspaces import router as workspaces_router
from app.config import get_settings
from app.database import async_session
from app.models.setting import Setting


async def seed_settings():
    """Seed non-LLM operational settings; LLM credentials live in providers/llm_models."""
    settings = get_settings()
    defaults = {
        "max_concurrent_agents": 3,
        "task_timeout_minutes": 60,
        "max_retries": 1,
        "embedding_provider": "fastembed",
        "embedding_model_local": "BAAI/bge-small-en-v1.5",
        "embedding_api_url": "",
        "embedding_api_key": "",
        "embedding_model_api": "",
        "minio_endpoint": settings.minio_endpoint,
        "minio_access_key": settings.minio_access_key,
        "minio_secret_key": settings.minio_secret_key,
        "memory_mode": "flat",
        # Quality Data Lake (E-01)
        "data_lake_retention_days": 0,  # 0 = keep forever
        "data_lake_public_opt_in_default": False,  # privacy: opt-in off by default
        # Quality Rubric Engine (E-02): auto-evaluate off by default to avoid
        # surprise token spend; the on-demand evaluate button works regardless.
        "quality_eval_enabled": False,
    }
    async with async_session() as db:
        for key, value in defaults.items():
            existing = await db.get(Setting, key)
            if not existing:
                db.add(Setting(key=key, value=value))
        await db.commit()


async def seed_default_provider():
    """Create one default Provider+Model in default workspace from env, if no providers exist there.

    Also assigns it to the workspace's three system_*_model_id FKs.
    """
    from sqlalchemy import func, select
    from app.models.provider import LLMModel, Provider
    from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace

    settings = get_settings()
    if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
        return  # nothing to seed; user will configure via UI

    async with async_session() as db:
        existing = await db.scalar(
            select(func.count())
            .select_from(Provider)
            .where(Provider.workspace_id == DEFAULT_WORKSPACE_ID)
        )
        if existing and existing > 0:
            return

        provider = Provider(
            workspace_id=DEFAULT_WORKSPACE_ID,
            name="default",
            api_key=settings.llm_api_key,
            endpoint=settings.llm_base_url,
        )
        db.add(provider)
        await db.flush()

        model = LLMModel(
            provider_id=provider.id,
            display_name=settings.llm_model,
            api_name=settings.llm_model,
        )
        db.add(model)
        await db.flush()

        workspace = await db.get(Workspace, DEFAULT_WORKSPACE_ID)
        if workspace is not None:
            if workspace.orchestrator_model_id is None:
                workspace.orchestrator_model_id = model.id
            if workspace.chat_model_id is None:
                workspace.chat_model_id = model.id
            if workspace.memory_extractor_model_id is None:
                workspace.memory_extractor_model_id = model.id

        await db.commit()
        logging.getLogger(__name__).info("Seeded default provider+model from env")


async def seed_templates():
    """Seed 5 default templates into the default workspace if none exist there.

    Templates inherit the workspace's orchestrator_model_id (default model) — if no
    model is configured yet, they are seeded without one and the user can pick later.
    """
    from sqlalchemy import func, select
    from app.models.template import Template
    from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace

    async with async_session() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(Template)
            .where(Template.workspace_id == DEFAULT_WORKSPACE_ID)
        )
        if count and count > 0:
            return

        workspace = await db.get(Workspace, DEFAULT_WORKSPACE_ID)
        default_model_id = workspace.orchestrator_model_id if workspace else None

        # Seed the builtin Tool & MCP Registry (SPA-41) for the default workspace,
        # then have the templates reference entries by id rather than inline names.
        from app.models.registry_entry import RegistryEntry

        tool_id: dict = {}
        for name in ("bash", "file_read", "file_write"):
            entry = RegistryEntry(
                workspace_id=DEFAULT_WORKSPACE_ID, name=name, kind="builtin",
                config={}, secrets={}, created_by="seed",
            )
            db.add(entry)
            await db.flush()
            tool_id[name] = str(entry.id)

        def _ids(*names):
            return [tool_id[n] for n in names]

        templates = [
            Template(
                name="Researcher",
                description="Searches the internet, analyzes findings, and creates research reports. Use for any information gathering tasks.",
                soul_md="You are an expert researcher. Search for information thoroughly, analyze it critically, and produce well-structured reports with sources.",
                model_id=default_model_id,
                tool_ids=_ids("bash", "file_write", "file_read"),
                tags=["research", "analysis"],
                workspace_id=DEFAULT_WORKSPACE_ID,
            ),
            Template(
                name="Writer",
                description="Writes texts: articles, posts, documentation, emails, creative writing. Use for any text creation tasks.",
                soul_md="You are a skilled writer. Write clear, engaging, well-structured texts. Adapt your style to the task: formal for docs, engaging for articles, concise for emails.",
                model_id=default_model_id,
                tool_ids=_ids("file_write", "file_read"),
                tags=["writing", "content"],
                workspace_id=DEFAULT_WORKSPACE_ID,
            ),
            Template(
                name="Coder",
                description="Writes and debugs code, creates scripts and utilities. Use for programming and software development tasks.",
                soul_md="You are an expert programmer. Write clean, well-tested, production-ready code. Use best practices. Always test your code before submitting.",
                model_id=default_model_id,
                tool_ids=_ids("bash", "file_read", "file_write"),
                tags=["coding", "programming"],
                workspace_id=DEFAULT_WORKSPACE_ID,
            ),
            Template(
                name="Analyst",
                description="Analyzes data, creates reports with insights and recommendations. Use for data analysis and business intelligence tasks.",
                soul_md="You are a data analyst. Analyze data thoroughly, find patterns and insights, and present clear recommendations with supporting evidence.",
                model_id=default_model_id,
                tool_ids=_ids("bash", "file_read", "file_write"),
                tags=["analysis", "data"],
                workspace_id=DEFAULT_WORKSPACE_ID,
            ),
            Template(
                name="Designer",
                description="Creates HTML pages, UI components, and web designs. Use for frontend design and prototyping tasks.",
                soul_md="You are a UI/UX designer who codes. Create beautiful, responsive HTML/CSS pages. Focus on clean design, good typography, and modern aesthetics.",
                model_id=default_model_id,
                tool_ids=_ids("bash", "file_write", "file_read"),
                tags=["design", "frontend"],
                workspace_id=DEFAULT_WORKSPACE_ID,
            ),
        ]

        for t in templates:
            db.add(t)
        await db.commit()
        logging.getLogger(__name__).info("Seeded 5 default templates")


async def seed_default_rubrics():
    """Seed the built-in quality rubrics (E-02) into the default workspace if absent.

    New workspaces clone these on registration (see app.api.auth.register).
    """
    from sqlalchemy import func, select
    from app.models.rubric import Rubric
    from app.models.workspace import DEFAULT_WORKSPACE_ID
    from app.quality.rubric import iter_default_rubrics

    async with async_session() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(Rubric)
            .where(Rubric.workspace_id == DEFAULT_WORKSPACE_ID)
        )
        if count and count > 0:
            return
        for _name, kwargs in iter_default_rubrics():
            db.add(Rubric(workspace_id=DEFAULT_WORKSPACE_ID, **kwargs))
        await db.commit()
        logging.getLogger(__name__).info("Seeded 5 default rubrics")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await seed_settings()
    await seed_default_provider()
    await seed_templates()
    await seed_default_rubrics()

    # Orchestrator + scheduler now run as separate worker containers (R3),
    # holding Postgres advisory locks so only one of each is leader.
    # The api process only owns event broadcasting (Redis pub/sub) and HTTP/WS.
    from app.utils.events import start_event_subscriber, stop_event_subscriber

    await start_event_subscriber()
    yield
    # Shutdown
    await stop_event_subscriber()


app = FastAPI(title="SpawnHive", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001", "http://localhost:3002"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


_AUDIT_SKIP_PATHS = {
    "/api/agent-webhook",  # internal webhook from agents
    "/api/events",  # noisy when fronted by WS
    "/api/auth",  # avoid leaking creds via audit log
}


@app.middleware("http")
async def audit_middleware(request, call_next):
    response = await call_next(request)
    try:
        method = request.method
        path = request.url.path
        if (
            method in ("POST", "PATCH", "PUT", "DELETE")
            and path.startswith("/api")
            and not any(path.startswith(p) for p in _AUDIT_SKIP_PATHS)
        ):
            from app.utils.events import log_event

            user = getattr(request.state, "user", None)
            workspace = getattr(request.state, "workspace", None)
            if workspace is None:
                # Audit only authenticated, workspace-scoped requests
                return response

            data = {"path": path, "method": method, "status": response.status_code}
            if user is not None:
                data["user_id"] = str(user.id)
                data["user_email"] = user.email

            async with async_session() as db:
                await log_event(
                    db, "user_action", "user", data,
                    workspace_id=workspace.id,
                )
    except Exception:
        pass
    return response

app.include_router(auth_router)
app.include_router(health_router)
app.include_router(settings_router)
app.include_router(tasks_router)
app.include_router(templates_router)
app.include_router(agents_router)
app.include_router(webhooks_router)
app.include_router(events_router)
app.include_router(events_ws_router)
app.include_router(chat_router)
app.include_router(knowledge_router)
app.include_router(memory_router)
app.include_router(analytics_router)
app.include_router(scheduled_jobs_router)
app.include_router(agent_logs_router)
app.include_router(agent_logs_ws_router)
app.include_router(providers_router)
app.include_router(models_router)
app.include_router(workspaces_router)
app.include_router(data_lake_router)
app.include_router(quality_router)
app.include_router(registry_router)
app.include_router(experiments_router)
