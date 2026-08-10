"""Initial production runtime schema.

Revision ID: 0001_production_runtime
Revises:
"""

from alembic import op


revision = "0001_production_runtime"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    ddl = """
        CREATE TABLE participants (
            id UUID PRIMARY KEY,
            participant_code VARCHAR(32) UNIQUE NOT NULL,
            student_no_ciphertext TEXT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            external_llm_consent_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE participant_invites (
            id UUID PRIMARY KEY,
            participant_id UUID NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
            token_hash TEXT UNIQUE NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE feishu_bindings (
            id UUID PRIMARY KEY,
            participant_id UUID UNIQUE NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
            app_id VARCHAR(128) NOT NULL,
            open_id VARCHAR(128) NOT NULL,
            chat_id VARCHAR(128) NULL,
            bound_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_feishu_app_open UNIQUE(app_id, open_id)
        );
        CREATE TABLE feishu_oauth_tokens (
            participant_id UUID PRIMARY KEY REFERENCES participants(id) ON DELETE CASCADE,
            access_token_ciphertext TEXT NOT NULL,
            refresh_token_ciphertext TEXT NOT NULL,
            access_token_expires_at TIMESTAMPTZ NOT NULL,
            refresh_token_expires_at TIMESTAMPTZ NULL,
            granted_scopes JSONB NULL,
            token_version INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE participant_profiles (
            id UUID PRIMARY KEY,
            participant_id UUID NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            profile_json JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_profile_version UNIQUE(participant_id, version)
        );
        CREATE TABLE state_observations (
            id UUID PRIMARY KEY,
            participant_id UUID NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
            observation_type VARCHAR(64) NOT NULL,
            source_message_id VARCHAR(128) NULL,
            payload_json JSONB NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_observation_message_type
                UNIQUE(participant_id, source_message_id, observation_type)
        );
        CREATE INDEX ix_observation_participant_time
            ON state_observations(participant_id, observed_at);
        CREATE TABLE prediction_runs (
            id UUID PRIMARY KEY,
            participant_id UUID NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
            profile_version INTEGER NULL,
            source_message_id VARCHAR(128) NULL,
            model_version VARCHAR(64) NOT NULL,
            input_snapshot_json JSONB NOT NULL,
            output_json JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_prediction_source_message
                UNIQUE(participant_id, source_message_id)
        );
        CREATE INDEX ix_prediction_participant_time
            ON prediction_runs(participant_id, created_at);
        CREATE TABLE conversation_messages (
            id UUID PRIMARY KEY,
            participant_id UUID NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
            feishu_message_id VARCHAR(128) NULL,
            role VARCHAR(16) NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_conversation_source_role
                UNIQUE(participant_id, feishu_message_id, role)
        );
        CREATE INDEX ix_conversation_participant_time
            ON conversation_messages(participant_id, created_at);
        CREATE TABLE bot_events (
            event_id VARCHAR(128) PRIMARY KEY,
            message_id VARCHAR(128) NULL,
            app_id VARCHAR(128) NOT NULL,
            open_id VARCHAR(128) NOT NULL,
            chat_id VARCHAR(128) NOT NULL,
            chat_type VARCHAR(32) NOT NULL DEFAULT 'p2p',
            text TEXT NOT NULL,
            message_created_at TIMESTAMPTZ NOT NULL,
            participant_id UUID NULL REFERENCES participants(id) ON DELETE SET NULL,
            status VARCHAR(32) NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            error_code VARCHAR(64) NULL,
            reply_text TEXT NULL,
            reply_message_id VARCHAR(128) NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ NULL
        );
        CREATE TABLE agent_runs (
            id UUID PRIMARY KEY,
            participant_id UUID NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
            message_id VARCHAR(128) NULL,
            model VARCHAR(128) NOT NULL,
            skill_version VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            finished_at TIMESTAMPTZ NULL
        );
        CREATE TABLE agent_tool_calls (
            id UUID PRIMARY KEY,
            agent_run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            tool_name VARCHAR(128) NOT NULL,
            arguments_summary_json JSONB NULL,
            result_summary_json JSONB NULL,
            status VARCHAR(32) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE feishu_device_flows (
            participant_id UUID PRIMARY KEY REFERENCES participants(id) ON DELETE CASCADE,
            device_code_ciphertext TEXT NOT NULL,
            user_code VARCHAR(128) NOT NULL,
            verification_url TEXT NOT NULL,
            interval_seconds INTEGER NOT NULL DEFAULT 5,
            expires_at TIMESTAMPTZ NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    for statement in ddl.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    for table in (
        "feishu_device_flows",
        "agent_tool_calls",
        "agent_runs",
        "bot_events",
        "conversation_messages",
        "prediction_runs",
        "state_observations",
        "participant_profiles",
        "feishu_oauth_tokens",
        "feishu_bindings",
        "participant_invites",
        "participants",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
