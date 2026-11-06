"""Durable benchmark trial journal and resource ownership.

Revision ID: bg02
Revises: bg01
"""

from alembic import op

revision = "bg02"
down_revision = "bg01"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE benchmark_trials (
            id UUID NOT NULL,
            workspace_id UUID NOT NULL,
            task_id UUID,
            dataset_case_id UUID,
            profile_id UUID,
            retry_of_id UUID,
            snapshot JSONB NOT NULL,
            generation INTEGER DEFAULT '1' NOT NULL,
            controller_id UUID,
            controller_expires_at TIMESTAMP WITH TIME ZONE,
            heartbeat_at TIMESTAMP WITH TIME ZONE,
            deadline_at TIMESTAMP WITH TIME ZONE NOT NULL,
            phase VARCHAR(30) DEFAULT 'created' NOT NULL,
            execution_status VARCHAR(30) DEFAULT 'pending' NOT NULL,
            evaluation_status VARCHAR(30) DEFAULT 'pending' NOT NULL,
            recording_status VARCHAR(30) DEFAULT 'open' NOT NULL,
            cleanup_status VARCHAR(30) DEFAULT 'pending' NOT NULL,
            cancel_requested_at TIMESTAMP WITH TIME ZONE,
            next_seq BIGINT DEFAULT '0' NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            finished_at TIMESTAMP WITH TIME ZONE,
            PRIMARY KEY (id),
            CONSTRAINT ck_benchmark_trial_counters CHECK (generation > 0 AND next_seq >= 0),
            CONSTRAINT ck_benchmark_trial_phase CHECK (phase IN ('created','planning','preparing','ready','driving','capturing','evaluating','cleaning','finished')),
            FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
            FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE SET NULL,
            FOREIGN KEY(dataset_case_id) REFERENCES benchmark_dataset_cases (id) ON DELETE RESTRICT,
            FOREIGN KEY(profile_id) REFERENCES benchmark_profiles (id) ON DELETE SET NULL,
            FOREIGN KEY(retry_of_id) REFERENCES benchmark_trials (id) ON DELETE SET NULL
        )
    """)
    op.execute("""
        CREATE INDEX ix_benchmark_trials_workspace_id ON benchmark_trials (workspace_id)
    """)
    op.execute("""
        CREATE TABLE benchmark_environment_sessions (
            id UUID NOT NULL,
            trial_id UUID NOT NULL,
            workspace_id UUID NOT NULL,
            origin_generation INTEGER NOT NULL,
            purpose VARCHAR(30) NOT NULL,
            state VARCHAR(30) DEFAULT 'planned' NOT NULL,
            plan_digest VARCHAR(64) NOT NULL,
            private_plan BYTEA NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_benchmark_session_scope UNIQUE (id, trial_id, workspace_id, origin_generation),
            CONSTRAINT ck_benchmark_session_generation CHECK (origin_generation > 0),
            FOREIGN KEY(trial_id) REFERENCES benchmark_trials (id) ON DELETE CASCADE,
            FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE
        )
    """)
    op.execute("""
        CREATE INDEX ix_benchmark_environment_sessions_trial_id ON benchmark_environment_sessions (trial_id)
    """)
    op.execute("""
        CREATE TABLE benchmark_event_streams (
            id UUID NOT NULL,
            trial_id UUID NOT NULL,
            generation INTEGER NOT NULL,
            actor_id UUID NOT NULL,
            next_seq BIGINT DEFAULT '0' NOT NULL,
            checkpoint_seq BIGINT,
            final_seq BIGINT,
            PRIMARY KEY (id),
            CONSTRAINT uq_benchmark_stream_actor UNIQUE (trial_id, generation, actor_id),
            CONSTRAINT ck_benchmark_stream_sequence CHECK (next_seq >= 0),
            FOREIGN KEY(trial_id) REFERENCES benchmark_trials (id) ON DELETE CASCADE
        )
    """)
    op.execute("""
        CREATE TABLE benchmark_host_reservations (
            host_id VARCHAR(100) NOT NULL,
            trial_id UUID NOT NULL,
            generation INTEGER NOT NULL,
            controller_id UUID NOT NULL,
            budget JSONB NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            PRIMARY KEY (host_id),
            UNIQUE (trial_id),
            FOREIGN KEY(trial_id) REFERENCES benchmark_trials (id) ON DELETE RESTRICT
        )
    """)
    op.execute("""
        CREATE TABLE benchmark_journal (
            id UUID NOT NULL,
            trial_id UUID NOT NULL,
            seq BIGINT NOT NULL,
            generation INTEGER NOT NULL,
            stream_id UUID,
            source_seq BIGINT,
            event_id VARCHAR(200),
            content_hash VARCHAR(64),
            event VARCHAR(100) NOT NULL,
            public_data JSONB DEFAULT '{}' NOT NULL,
            private_data BYTEA,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_benchmark_journal_seq UNIQUE (trial_id, seq),
            CONSTRAINT uq_benchmark_journal_source_seq UNIQUE (stream_id, source_seq),
            CONSTRAINT uq_benchmark_journal_event_id UNIQUE (stream_id, event_id),
            FOREIGN KEY(trial_id) REFERENCES benchmark_trials (id) ON DELETE CASCADE,
            FOREIGN KEY(stream_id) REFERENCES benchmark_event_streams (id) ON DELETE CASCADE
        )
    """)
    op.execute("""
        CREATE INDEX ix_benchmark_journal_trial_id ON benchmark_journal (trial_id)
    """)
    op.execute("""
        CREATE TABLE benchmark_operations (
            id UUID NOT NULL,
            trial_id UUID NOT NULL,
            session_id UUID NOT NULL,
            generation INTEGER NOT NULL,
            actor_id UUID NOT NULL,
            request_id VARCHAR(200) NOT NULL,
            content_hash VARCHAR(64) NOT NULL,
            state VARCHAR(30) NOT NULL,
            private_request BYTEA NOT NULL,
            private_result BYTEA,
            error_code VARCHAR(100),
            error_kind VARCHAR(30),
            started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            finished_at TIMESTAMP WITH TIME ZONE,
            PRIMARY KEY (id),
            CONSTRAINT uq_benchmark_operation_identity UNIQUE (trial_id, session_id, generation, actor_id, request_id),
            FOREIGN KEY(trial_id) REFERENCES benchmark_trials (id) ON DELETE CASCADE,
            FOREIGN KEY(session_id) REFERENCES benchmark_environment_sessions (id) ON DELETE CASCADE
        )
    """)
    op.execute("""
        CREATE INDEX ix_benchmark_operations_trial_id ON benchmark_operations (trial_id)
    """)
    op.execute("""
        CREATE TABLE benchmark_resource_leases (
            id UUID NOT NULL,
            session_id UUID NOT NULL,
            trial_id UUID NOT NULL,
            workspace_id UUID NOT NULL,
            origin_generation INTEGER NOT NULL,
            logical_id VARCHAR(200) NOT NULL,
            provider_id VARCHAR(200) NOT NULL,
            kind VARCHAR(30) NOT NULL,
            state VARCHAR(30) NOT NULL,
            private_value BYTEA NOT NULL,
            deadline_at TIMESTAMP WITH TIME ZONE,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT fk_benchmark_lease_scope FOREIGN KEY(session_id, trial_id, workspace_id, origin_generation) REFERENCES benchmark_environment_sessions (id, trial_id, workspace_id, origin_generation) ON DELETE CASCADE,
            CONSTRAINT uq_benchmark_lease_logical UNIQUE (session_id, logical_id)
        )
    """)
    op.execute("""
        CREATE INDEX ix_benchmark_resource_leases_trial_id ON benchmark_resource_leases (trial_id)
    """)

    op.execute("""
        CREATE FUNCTION evalhive_benchmark_immutable_journal() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Benchmark journal entries are immutable';
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER immutable_benchmark_journal BEFORE UPDATE ON benchmark_journal
        FOR EACH ROW EXECUTE FUNCTION evalhive_benchmark_immutable_journal();
    """)
    op.execute("""
        CREATE FUNCTION evalhive_benchmark_frozen_inputs() RETURNS trigger AS $$
        BEGIN
            IF TG_TABLE_NAME = 'benchmark_trials' THEN
                IF ROW(OLD.id, OLD.workspace_id, OLD.dataset_case_id, OLD.snapshot, OLD.deadline_at)
                   IS DISTINCT FROM
                   ROW(NEW.id, NEW.workspace_id, NEW.dataset_case_id, NEW.snapshot, NEW.deadline_at) THEN
                    RAISE EXCEPTION 'Benchmark trial inputs are immutable';
                END IF;
            ELSE
                IF ROW(OLD.id, OLD.trial_id, OLD.workspace_id, OLD.origin_generation,
                       OLD.purpose, OLD.plan_digest, OLD.private_plan)
                   IS DISTINCT FROM
                   ROW(NEW.id, NEW.trial_id, NEW.workspace_id, NEW.origin_generation,
                       NEW.purpose, NEW.plan_digest, NEW.private_plan) THEN
                    RAISE EXCEPTION 'Benchmark session plan is immutable';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER frozen_benchmark_trial BEFORE UPDATE ON benchmark_trials
        FOR EACH ROW EXECUTE FUNCTION evalhive_benchmark_frozen_inputs();
    """)
    op.execute("""
        CREATE TRIGGER frozen_benchmark_session BEFORE UPDATE ON benchmark_environment_sessions
        FOR EACH ROW EXECUTE FUNCTION evalhive_benchmark_frozen_inputs();
    """)


def downgrade():
    op.execute("DROP FUNCTION evalhive_benchmark_immutable_journal() CASCADE")
    op.execute("DROP FUNCTION evalhive_benchmark_frozen_inputs() CASCADE")
    for name in (
        "benchmark_resource_leases",
        "benchmark_operations",
        "benchmark_journal",
        "benchmark_event_streams",
        "benchmark_environment_sessions",
        "benchmark_host_reservations",
        "benchmark_trials",
    ):
        op.drop_table(name)
