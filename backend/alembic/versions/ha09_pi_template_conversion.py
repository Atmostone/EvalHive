"""Pin existing templates to Pi without rewriting historical execution evidence.

Revision ID: ha09
Revises: ha08
Create Date: 2026-11-05
"""

import sqlalchemy as sa
from alembic import op

revision = "ha09"
down_revision = "ha08"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    unsupported = connection.scalar(sa.text("""
        SELECT count(*) FROM harness_profiles p
        WHERE p.connector_id IN ('legacy', 'legacy-toolathlon')
          AND (p.config <> '{}'::jsonb OR p.secret_refs <> '{}'::jsonb)
    """))
    if unsupported:
        raise RuntimeError("Legacy template profiles require explicit settings conversion before upgrading")
    # One neutral Pi profile per workspace. Preserve user-selected defaults and
    # explicitly configured connectors; this migration only replaces legacy intent.
    op.execute("""
        INSERT INTO harness_profiles (id, workspace_id, name, connector_id, connector_version, is_default)
        SELECT md5(w.id::text || '-h15-pi')::uuid, w.id, 'Pi migration ' || left(w.id::text, 8), 'pi', '0.1.0', false
        FROM workspaces w WHERE NOT EXISTS (
            SELECT 1 FROM harness_profiles p WHERE p.workspace_id = w.id
              AND p.connector_id = 'pi' AND p.connector_version = '0.1.0'
              AND p.config = '{}'::jsonb AND p.secret_refs = '{}'::jsonb AND p.model_id IS NULL
        )
    """)
    op.create_table(
        "harness_template_conversions",
        sa.Column("template_id", sa.UUID(), sa.ForeignKey("templates.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_profile_id", sa.UUID()),
        sa.Column("target_profile_id", sa.UUID(), nullable=False),
        sa.Column("source_model_id", sa.UUID()),
        sa.Column("prior_updated_at", sa.DateTime()),
        sa.Column("converted_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    # A legacy profile's model remains explicit on the template. Empty native
    # settings map to Pi defaults; unsupported settings already aborted above.
    op.execute("""
        INSERT INTO harness_template_conversions
            (template_id, workspace_id, source_profile_id, target_profile_id, source_model_id, prior_updated_at)
        SELECT t.id, t.workspace_id, t.harness_profile_id,
            (SELECT p.id FROM harness_profiles p WHERE p.workspace_id = t.workspace_id
              AND p.connector_id = 'pi' AND p.connector_version = '0.1.0'
              AND ((t.harness_profile_id IS NULL AND p.is_default) OR
                   (p.config = '{}'::jsonb AND p.secret_refs = '{}'::jsonb AND p.model_id IS NULL))
              ORDER BY p.is_default DESC, p.created_at, p.id LIMIT 1),
            t.model_id, t.updated_at
        FROM templates t LEFT JOIN harness_profiles old ON old.id = t.harness_profile_id
        WHERE t.harness_profile_id IS NULL OR old.connector_id IN ('legacy', 'legacy-toolathlon')
    """)
    op.execute("""
        UPDATE templates t SET harness_profile_id = c.target_profile_id,
            model_id = COALESCE(p.model_id, t.model_id)
        FROM harness_template_conversions c LEFT JOIN harness_profiles p ON p.id = c.source_profile_id
        WHERE t.id = c.template_id
    """)
    op.create_table(
        "harness_profile_conversions",
        sa.Column("profile_id", sa.UUID(), sa.ForeignKey("harness_profiles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("source_connector_id", sa.String(200), nullable=False),
        sa.Column("source_connector_version", sa.String(100), nullable=False),
        sa.Column("prior_updated_at", sa.DateTime()),
    )
    op.execute("""
        INSERT INTO harness_profile_conversions
        SELECT id, connector_id, connector_version, updated_at FROM harness_profiles
        WHERE connector_id IN ('legacy', 'legacy-toolathlon')
    """)
    # Keep existing default/model references usable after legacy is uninstalled.
    op.execute("""
        UPDATE harness_profiles SET connector_id = 'pi', connector_version = '0.1.0'
        WHERE id IN (SELECT profile_id FROM harness_profile_conversions)
    """)


def downgrade():
    # Do not overwrite a template edited after migration. Historical versions,
    # attempts and bundles are never rewritten in either direction.
    op.execute("""
        UPDATE harness_profiles p SET connector_id = c.source_connector_id,
            connector_version = c.source_connector_version
        FROM harness_profile_conversions c
        WHERE p.id = c.profile_id AND p.connector_id = 'pi' AND p.connector_version = '0.1.0'
          AND p.updated_at IS NOT DISTINCT FROM c.prior_updated_at
    """)
    op.drop_table("harness_profile_conversions")
    op.execute("""
        UPDATE templates t SET harness_profile_id = c.source_profile_id, model_id = c.source_model_id
        FROM harness_template_conversions c
        WHERE t.id = c.template_id AND t.harness_profile_id = c.target_profile_id
          AND t.updated_at IS NOT DISTINCT FROM c.prior_updated_at
          AND (c.source_profile_id IS NULL OR EXISTS (SELECT 1 FROM harness_profiles p WHERE p.id = c.source_profile_id))
    """)
    op.drop_table("harness_template_conversions")
    op.execute("""
        DELETE FROM harness_profiles p
        WHERE p.id = md5(p.workspace_id::text || '-h15-pi')::uuid
          AND NOT p.is_default
          AND NOT EXISTS (SELECT 1 FROM templates t WHERE t.harness_profile_id = p.id)
          AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.harness_profile_id = p.id)
          AND NOT EXISTS (SELECT 1 FROM execution_attempts a WHERE a.profile_id = p.id)
    """)
