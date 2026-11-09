"""
003_add_evidence_and_playbooks.py — PostgreSQL Execution Evidence & Playbooks Migration
========================================================================================
Phase 3: Creates tables for:
1. execution_evidence:
   - Cryptographically hashed evidence records (SHA-256)
   - Strongly bound to user_id, session_id, plan_id, plan_hash, and task_id
   - Captures verification results, confidence scores, and redacted payloads
2. playbooks:
   - Reusable verified execution workflows
   - Strongly bound to user_id (tenant isolation) and provenance plan hash
   - Life-cycle status tracking: candidate, promoted, deprecated, revoked
   - Preconditions, required tools, and expected outcomes
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

MIGRATION_SQL = """
-- 1. Execution Evidence Table
CREATE TABLE IF NOT EXISTS execution_evidence (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id VARCHAR(64) NOT NULL,
    plan_id VARCHAR(64) NOT NULL,
    plan_hash VARCHAR(64) NOT NULL,
    task_id VARCHAR(64) NOT NULL,
    task_name TEXT NOT NULL,
    evidence_type VARCHAR(32) NOT NULL,
    artifact_hash VARCHAR(64) NOT NULL,
    captured_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    verification_status VARCHAR(32) NOT NULL DEFAULT 'inconclusive',
    verification_reason TEXT NULL,
    confidence_score FLOAT NOT NULL DEFAULT 0.0,
    metadata JSONB DEFAULT '{}',
    content_payload TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_execution_evidence_session ON execution_evidence (session_id);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_user_session ON execution_evidence (user_id, session_id);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_task ON execution_evidence (session_id, task_id);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_hash ON execution_evidence (artifact_hash);

-- 2. Playbooks Table (Controlled Reusable Knowledge)
CREATE TABLE IF NOT EXISTS playbooks (
    id VARCHAR(64) PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    user_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workflow_category VARCHAR(64) NOT NULL,
    goal_pattern TEXT NOT NULL,
    preconditions JSONB DEFAULT '[]',
    execution_steps JSONB NOT NULL DEFAULT '[]',
    required_tools JSONB NOT NULL DEFAULT '[]',
    expected_outcomes JSONB NOT NULL DEFAULT '[]',
    supporting_evidence_ids JSONB DEFAULT '[]',
    provenance_session_id VARCHAR(64) NOT NULL,
    provenance_plan_hash VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'candidate',
    confidence_score FLOAT NOT NULL DEFAULT 0.5,
    success_count INTEGER NOT NULL DEFAULT 1,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NULL,
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_playbooks_user_category ON playbooks (user_id, workflow_category);
CREATE INDEX IF NOT EXISTS idx_playbooks_status ON playbooks (status);
CREATE INDEX IF NOT EXISTS idx_playbooks_provenance_hash ON playbooks (provenance_plan_hash);
"""

ROLLBACK_SQL = """
DROP INDEX IF EXISTS idx_playbooks_provenance_hash;
DROP INDEX IF EXISTS idx_playbooks_status;
DROP INDEX IF EXISTS idx_playbooks_user_category;
DROP TABLE IF EXISTS playbooks;

DROP INDEX IF EXISTS idx_execution_evidence_hash;
DROP INDEX IF EXISTS idx_execution_evidence_task;
DROP INDEX IF EXISTS idx_execution_evidence_user_session;
DROP INDEX IF EXISTS idx_execution_evidence_session;
DROP TABLE IF EXISTS execution_evidence;
"""


def apply_migration(connection_uri: Optional[str] = None) -> bool:
    """Applies the execution_evidence and playbooks migration to PostgreSQL."""
    uri = connection_uri or os.getenv("POSTGRES_URI") or os.getenv("DATABASE_URL")
    if not uri:
        logger.warning("[Migration 003] No POSTGRES_URI provided. Skipping DB migration.")
        return False

    try:
        import psycopg2
        conn = psycopg2.connect(uri, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(MIGRATION_SQL)
        conn.close()
        logger.info("[Migration 003] Successfully applied execution_evidence and playbooks migration.")
        return True
    except Exception as e:
        logger.error(f"[Migration 003] Migration failed: {e}")
        return False


def rollback_migration(connection_uri: Optional[str] = None) -> bool:
    """Rolls back the execution_evidence and playbooks migration."""
    uri = connection_uri or os.getenv("POSTGRES_URI") or os.getenv("DATABASE_URL")
    if not uri:
        logger.warning("[Migration 003 Rollback] No POSTGRES_URI provided.")
        return False

    try:
        import psycopg2
        conn = psycopg2.connect(uri, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(ROLLBACK_SQL)
        conn.close()
        logger.info("[Migration 003 Rollback] Successfully rolled back migration 003.")
        return True
    except Exception as e:
        logger.error(f"[Migration 003 Rollback] Rollback failed: {e}")
        return False


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    apply_migration()

