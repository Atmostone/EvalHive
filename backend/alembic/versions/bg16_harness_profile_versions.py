"""Upgrade compatible built-in profile pins without changing execution history.

Revision ID: bg16
Revises: bg15
"""

import json

import sqlalchemy as sa
from alembic import op
from jsonschema import Draft202012Validator

revision = "bg16"
down_revision = "bg15"
branch_labels = None
depends_on = None

TARGETS = {"pi": "0.2.1", "opencode": "0.2.2"}
SOURCES = {"pi": {"0.1.0", "0.2.0"}, "opencode": {"0.1.0", "0.2.0", "0.2.1"}}
# Frozen release schemas keep this migration independent of future manifests.
SCHEMAS = json.loads(r"""{
  "pi": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "thinkingLevel": {
        "type": "string",
        "enum": [
          "off",
          "minimal",
          "low",
          "medium",
          "high",
          "xhigh",
          "max"
        ],
        "default": "off"
      },
      "contextWindow": {
        "type": "integer",
        "minimum": 128,
        "maximum": 2000000,
        "default": 128000
      },
      "maxOutputTokens": {
        "type": "integer",
        "minimum": 1,
        "maximum": 128000,
        "default": 8192
      },
      "autoCompaction": {
        "type": "boolean",
        "default": true
      },
      "maxRetries": {
        "type": "integer",
        "minimum": 0,
        "maximum": 5,
        "default": 2
      },
      "retryBaseDelayMs": {
        "type": "integer",
        "minimum": 1,
        "maximum": 30000,
        "default": 200
      },
      "temperature": {
        "type": "number",
        "minimum": 0,
        "maximum": 2
      },
      "seed": {
        "type": "integer",
        "minimum": 0,
        "maximum": 9007199254740991
      },
      "reasoning": {
        "type": "boolean",
        "default": false
      },
      "compat": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "supportsUsageInStreaming": {
            "type": "boolean"
          },
          "supportsReasoningEffort": {
            "type": "boolean"
          },
          "maxTokensField": {
            "enum": [
              "max_tokens",
              "max_completion_tokens"
            ]
          },
          "thinkingFormat": {
            "enum": [
              "deepseek",
              "openai",
              "qwen",
              "qwen-chat-template",
              "zai"
            ]
          }
        }
      },
      "compaction": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "reserveTokens": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1999999,
            "default": 16384
          },
          "keepRecentTokens": {
            "type": "integer",
            "minimum": 0,
            "maximum": 2000000,
            "default": 20000
          }
        }
      }
    }
  },
  "opencode": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "apiTimeoutSeconds": {
        "type": "integer",
        "minimum": 1,
        "maximum": 120,
        "default": 30,
        "description": "Budget for native server observations; the attempt deadline still applies."
      },
      "contextWindow": {
        "type": "integer",
        "minimum": 128,
        "maximum": 2000000,
        "default": 128000
      },
      "maxOutputTokens": {
        "type": "integer",
        "minimum": 1,
        "maximum": 128000,
        "default": 8192
      },
      "autoCompaction": {
        "type": "boolean",
        "default": true
      },
      "temperature": {
        "type": "number",
        "minimum": 0,
        "maximum": 2
      },
      "reasoning": {
        "type": "boolean",
        "default": false
      },
      "topP": {
        "type": "number",
        "minimum": 0,
        "maximum": 1
      },
      "permissionMode": {
        "enum": [
          "allow",
          "ask",
          "deny"
        ],
        "default": "allow"
      }
    }
  }
}""")
SETTINGS_DIGEST = "md5(p.config::text || p.secret_refs::text || coalesce(p.model_id::text, ''))"


def validate_config(connector, config):
    if not Draft202012Validator(SCHEMAS[connector]).is_valid(config):
        return False
    context = config.get("contextWindow", 128000)
    if config.get("maxOutputTokens", 8192) > context:
        return False
    if connector == "pi" and config.get("autoCompaction", True):
        if config.get("compaction", {}).get("reserveTokens", 16384) >= context:
            return False
    return True


def upgrade():
    connection = op.get_bind()
    rows = (
        connection.execute(
            sa.text(
                "SELECT id, connector_id, connector_version, config FROM harness_profiles "
                "WHERE connector_id IN ('pi','opencode') FOR UPDATE"
            )
        )
        .mappings()
        .all()
    )
    selected = []
    for row in rows:
        connector, version = row["connector_id"], row["connector_version"]
        if version == TARGETS[connector]:
            continue
        if version not in SOURCES[connector] or not validate_config(connector, row["config"]):
            # Never include settings or validator exceptions in migration output.
            raise RuntimeError(
                f"Harness profile {row['id']} requires explicit version/settings conversion"
            )
        selected.append(row)
    op.create_table(
        "harness_profile_version_upgrades",
        sa.Column(
            "profile_id",
            sa.UUID(),
            sa.ForeignKey("harness_profiles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "workspace_id",
            sa.UUID(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("connector_id", sa.String(200), nullable=False),
        sa.Column("source_version", sa.String(100), nullable=False),
        sa.Column("target_version", sa.String(100), nullable=False),
        sa.Column("settings_digest", sa.String(32), nullable=False),
        sa.Column("prior_updated_at", sa.DateTime()),
        sa.Column("converted_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    for row in selected:
        connection.execute(
            sa.text(
                "INSERT INTO harness_profile_version_upgrades "
                "(profile_id, workspace_id, connector_id, source_version, target_version, settings_digest, prior_updated_at) "
                "SELECT p.id, p.workspace_id, p.connector_id, p.connector_version, :target, "
                + SETTINGS_DIGEST
                + ", p.updated_at FROM harness_profiles p WHERE p.id=:id"
            ),
            {"id": row["id"], "target": TARGETS[row["connector_id"]]},
        )
        connection.execute(
            sa.text("UPDATE harness_profiles SET connector_version=:target WHERE id=:id"),
            {"id": row["id"], "target": TARGETS[row["connector_id"]]},
        )


def downgrade():
    connection = op.get_bind()
    unchanged = (
        "p.workspace_id=c.workspace_id AND p.connector_id=c.connector_id "
        "AND p.connector_version=c.target_version "
        "AND p.updated_at IS NOT DISTINCT FROM c.prior_updated_at "
        "AND " + SETTINGS_DIGEST + "=c.settings_digest"
    )
    changed = connection.scalar(
        sa.text(
            "SELECT count(*) FROM harness_profile_version_upgrades c "
            "JOIN harness_profiles p ON p.id=c.profile_id WHERE NOT (" + unchanged + ")"
        )
    )
    if changed:
        raise RuntimeError(
            "Edited harness profiles require the paired database backup for rollback"
        )
    connection.execute(
        sa.text(
            "UPDATE harness_profiles p SET connector_version=c.source_version "
            "FROM harness_profile_version_upgrades c WHERE p.id=c.profile_id AND " + unchanged
        )
    )
    op.drop_table("harness_profile_version_upgrades")
