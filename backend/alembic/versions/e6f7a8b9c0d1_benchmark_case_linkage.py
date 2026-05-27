"""benchmark case linkage — Benchmark Case Store (pre-E-23)

Links a runnable task instance (and its denormalized quality record) back to the
benchmark case it was materialized from, so results can be aggregated by suite ×
case × model. The case *definitions* live in versioned files (`backend/benchmarks/`)
— no registry table yet (that, plus the API/UI and publication, is E-23). These are
just the linkage columns.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-05-26
"""

from alembic import op
import sqlalchemy as sa


revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade():
    # Source of truth for spawn lineage — set when a case is materialized.
    op.add_column("tasks", sa.Column("benchmark_case_id", sa.String(128), nullable=True))
    op.add_column("tasks", sa.Column("benchmark_suite", sa.String(128), nullable=True))
    # Denormalized onto the quality record (E-01 philosophy: survives task deletion;
    # the suite is indexed for aggregation grouping).
    op.add_column("quality_records", sa.Column("benchmark_case_id", sa.String(128), nullable=True))
    op.add_column("quality_records", sa.Column("benchmark_suite", sa.String(128), nullable=True))
    op.create_index(
        "idx_quality_records_benchmark_suite", "quality_records", ["benchmark_suite"]
    )


def downgrade():
    op.drop_index("idx_quality_records_benchmark_suite", table_name="quality_records")
    op.drop_column("quality_records", "benchmark_suite")
    op.drop_column("quality_records", "benchmark_case_id")
    op.drop_column("tasks", "benchmark_suite")
    op.drop_column("tasks", "benchmark_case_id")
