import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Rubric(Base):
    """A multi-dimensional quality rubric (E-02).

    A rubric is a set of independent quality dimensions used to score a task's
    result into a profile (vector), not a single number. Each dimension declares
    its own evaluator; only ``judge`` (LLM-as-judge, O2) is wired today —
    ``objective`` (E-04 probes) and ``human`` (E-05) are recognized but deferred.

    Selection precedence for a task: ``Template.rubric_id`` → a rubric whose
    ``applies_to`` matches a template tag → the workspace's ``is_default`` rubric.

    ``dimensions`` is a list of dicts::

        {"key": str, "name": str, "description": str,
         "evaluator": "judge"|"objective"|"human",
         "weight": float, "threshold": int (0-10), "critical": bool}
    """

    __tablename__ = "rubrics"
    __table_args__ = (Index("idx_rubrics_workspace", "workspace_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Task-type tag for auto-selection (e.g. code/report/content/design/data).
    applies_to: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # The workspace's last-resort rubric when nothing else matches.
    is_default: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    dimensions: Mapped[list] = mapped_column(JSONB, default=list, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
