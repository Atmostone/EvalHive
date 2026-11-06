"""Rubrics for the Multi-dimensional Quality Rubric Engine (E-02).

A rubric is a set of independent quality dimensions. ``DEFAULT_RUBRICS`` are the
built-ins seeded into the default workspace (and cloned to each new one);
``resolve_rubric_for_task`` picks the rubric to score a task with.

Each dimension declares its own evaluator. Only ``judge`` (LLM-as-judge) is wired
today; ``objective`` (E-04) and ``human`` (E-05) are valid in the schema but
scored as ``deferred`` until those subsystems land. The defaults therefore use
``judge`` everywhere so the profile is fully populated out of the box.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rubric import Rubric
from app.models.task import Task
from app.models.template import Template


def _dim(key, name, description, *, weight, threshold, critical=False, evaluator="judge"):
    return {
        "key": key,
        "name": name,
        "description": description,
        "evaluator": evaluator,
        "weight": weight,
        "threshold": threshold,
        "critical": critical,
    }


# Built-in rubrics. ``applies_to`` matches a default-template tag so a task
# without an explicit rubric_id still gets a sensible rubric (see resolution).
DEFAULT_RUBRICS: list[dict] = [
    {
        "name": "Analytical Report",
        "description": "Quality profile for research reports and analytical write-ups.",
        "applies_to": "analysis",
        "is_default": True,
        "dimensions": [
            _dim("factual_accuracy", "Factual Accuracy", "Are claims correct and supported by evidence?", weight=0.25, threshold=6, critical=True),
            _dim("completeness", "Completeness", "Does it fully cover the question without notable gaps?", weight=0.20, threshold=5),
            _dim("structure", "Structure", "Logical organization, clear sections and flow.", weight=0.15, threshold=5),
            _dim("source_quality", "Source Quality", "Credibility and relevance of cited sources.", weight=0.15, threshold=5),
            _dim("originality", "Originality", "Original synthesis vs. shallow restatement.", weight=0.10, threshold=4),
            _dim("readability", "Readability", "Clear, concise, audience-appropriate language.", weight=0.15, threshold=5),
        ],
    },
    {
        "name": "Code",
        "description": "Quality profile for code and software-development results.",
        "applies_to": "coding",
        "is_default": False,
        "dimensions": [
            _dim("correctness", "Correctness", "Does the code do what was asked and handle the cases?", weight=0.30, threshold=6, critical=True),
            _dim("code_style", "Style", "Idiomatic, consistent, readable code.", weight=0.15, threshold=5),
            _dim("maintainability", "Maintainability", "Simplicity, modularity, low complexity.", weight=0.15, threshold=5),
            _dim("error_handling", "Error Handling", "Robustness to invalid input and failures.", weight=0.15, threshold=5),
            _dim("test_coverage", "Test Coverage", "Adequacy of tests for the delivered code.", weight=0.10, threshold=5),
            _dim("documentation", "Documentation", "Docstrings/comments and usage clarity.", weight=0.15, threshold=4),
        ],
    },
    {
        "name": "Content",
        "description": "Quality profile for articles, posts, docs and other written content.",
        "applies_to": "content",
        "is_default": False,
        "dimensions": [
            _dim("clarity", "Clarity", "Is the message clear and easy to follow?", weight=0.25, threshold=6, critical=True),
            _dim("structure", "Structure", "Logical flow, headings, pacing.", weight=0.15, threshold=5),
            _dim("engagement", "Engagement", "Is it compelling and audience-appropriate?", weight=0.15, threshold=5),
            _dim("correctness", "Correctness", "Grammar, spelling and factual accuracy.", weight=0.20, threshold=5),
            _dim("tone", "Tone", "Tone fits the purpose (formal/engaging/concise).", weight=0.10, threshold=5),
            _dim("originality", "Originality", "Fresh angle vs. generic filler.", weight=0.15, threshold=4),
        ],
    },
    {
        "name": "Design",
        "description": "Quality profile for HTML/UI pages and web design results.",
        "applies_to": "design",
        "is_default": False,
        "dimensions": [
            _dim("visual_design", "Visual Design", "Aesthetics, layout, typography, spacing.", weight=0.25, threshold=6, critical=True),
            _dim("responsiveness", "Responsiveness", "Adapts across breakpoints / viewports.", weight=0.20, threshold=5),
            _dim("accessibility", "Accessibility", "Semantics, contrast, alt text, a11y basics.", weight=0.15, threshold=5),
            _dim("code_quality", "Code Quality", "Clean, valid HTML/CSS.", weight=0.15, threshold=5),
            _dim("content_clarity", "Content Clarity", "Clear, well-organized on-page content.", weight=0.10, threshold=5),
            _dim("consistency", "Consistency", "Consistent design language across the page.", weight=0.15, threshold=5),
        ],
    },
    {
        "name": "Data Analysis",
        "description": "Quality profile for data-analysis tasks and BI deliverables.",
        "applies_to": "data",
        "is_default": False,
        "dimensions": [
            _dim("analytical_rigor", "Analytical Rigor", "Sound method, valid reasoning, no overreach.", weight=0.25, threshold=6, critical=True),
            _dim("correctness", "Correctness", "Calculations and figures are accurate.", weight=0.20, threshold=6, critical=True),
            _dim("completeness", "Completeness", "Covers the analysis the task required.", weight=0.15, threshold=5),
            _dim("insightfulness", "Insightfulness", "Actionable, non-obvious findings.", weight=0.15, threshold=5),
            _dim("visualization", "Visualization", "Clear, appropriate charts/tables (if any).", weight=0.10, threshold=4),
            _dim("clarity", "Clarity", "Findings presented clearly with evidence.", weight=0.15, threshold=5),
        ],
    },
    {
        "name": "Tool Use / Data Task",
        "description": "Quality profile for agentic tool-use and data tasks (Toolathlon-style): correctness of produced artifacts, not prose craft.",
        "applies_to": "toolathlon",
        "is_default": False,
        "dimensions": [
            _dim("task_completion", "Task Completion", "Did the run produce ALL required deliverable artifacts, populated with real content (not placeholders/empty sheets), as specified? Every required file/email/event/db-row present and non-empty. Score 0 when no deliverables were produced.", weight=0.30, threshold=6, critical=True),
            _dim("output_accuracy", "Output Accuracy", "Are the concrete values correct: exact numbers (arithmetic, rounding to spec, cross-file/cross-system totals), correct rows/records, no fabricated data? Grade mechanical correctness like an executable checker. If there are NO artifacts to check, mark this axis not_applicable (task_completion already carries that penalty) — do not collapse to 0.", weight=0.30, threshold=6, critical=True),
            _dim("instruction_following", "Instruction Following", "Did the deliverables obey explicit task constraints: required sheet/column names, sort order (alphabetical/numeric), section ordering, filters/date ranges, truncation, naming.", weight=0.20, threshold=5),
            _dim("format_compliance", "Format Compliance", "Is each artifact a VALID instance of its declared format: a real iCalendar (BEGIN:VCALENDAR/VEVENT) not markdown-with-.ics, a true .docx/.xlsx not markdown text, no markdown/HTML bleed-through, no garbled encoding/mojibake, correct extension/MIME.", weight=0.12, threshold=5),
            _dim("presentation_clarity", "Presentation Clarity", "For artifacts with a free-prose target (docx reports, email bodies, slide narratives): clear, well-organized, audience-appropriate language with proper number formatting. Mark not_applicable when EVERY deliverable for the task is a pure structured/exact artifact (numbers-only xlsx, .ics, db rows) with no narrative target.", weight=0.08, threshold=4),
        ],
    },
]


def iter_default_rubrics():
    """Yield ``(name, kwargs)`` for building Rubric rows in a given workspace."""
    for r in DEFAULT_RUBRICS:
        yield r["name"], {
            "name": r["name"],
            "description": r["description"],
            "applies_to": r["applies_to"],
            "is_default": r["is_default"],
            "dimensions": [dict(d) for d in r["dimensions"]],
        }


async def resolve_rubric_for_task(db: AsyncSession, task: Task) -> Rubric | None:
    """Pick the rubric to score ``task`` with.

    Precedence: the task template's explicit ``rubric_id`` → a workspace rubric
    whose ``applies_to`` matches one of the template's tags (in tag order) → the
    workspace's ``is_default`` rubric → ``None`` (evaluation is skipped).
    """
    template: Template | None = None
    if task.template_id:
        template = await db.get(Template, task.template_id)

    if template is not None and template.rubric_id:
        rubric = await db.get(Rubric, template.rubric_id)
        if rubric is not None and rubric.workspace_id == task.workspace_id:
            return rubric

    if template is not None and template.tags:
        for tag in template.tags:
            rubric = (
                await db.execute(
                    select(Rubric)
                    .where(
                        Rubric.workspace_id == task.workspace_id,
                        Rubric.applies_to == tag,
                    )
                    .order_by(Rubric.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if rubric is not None:
                return rubric

    return (
        await db.execute(
            select(Rubric)
            .where(
                Rubric.workspace_id == task.workspace_id,
                Rubric.is_default.is_(True),
            )
            .order_by(Rubric.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
