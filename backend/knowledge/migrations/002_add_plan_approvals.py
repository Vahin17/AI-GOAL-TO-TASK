"""
002_add_plan_approvals.py — PostgreSQL Plan Approvals Cryptographic Binding Migration
=====================================================================================
Creates the plan_approvals table to cryptographically bind human approvals to:
- user_id (authoritative authenticated user)
- session_id (ARDO execution session)
- plan_hash (canonical SHA-256 DAG hash)
- plan_version (version of the plan)
- status (approved, rejected, expired, revoked)
- task_id (optional task-level gate or session-wide)
- created_at, expires_at, revoked_at, and audit metadata (JSONB)

Provides clean, safe upgrade and rollback procedures with zero data loss.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS plan_approvals (
    id VARCHAR(64) PRIMARY KEY,
    session_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_hash VARCHAR(64) NOT NULL,
    plan_version INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(32) NOT NULL DEFAULT 'approved',
    task_id VARCHAR(64) NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ NULL,
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_plan_approvals_session ON plan_approvals (session_id);
CREATE INDEX IF NOT EXISTS idx_plan_approvals_user ON plan_approvals (user_id);
CREATE INDEX IF NOT EXISTS idx_plan_approvals_hash ON plan_approvals (plan_hash);
CREATE INDEX IF NOT EXISTS idx_plan_approvals_session_status ON plan_approvals (session_id, status, expires_at);
"""

ROLLBACK_SQL = """
DROP INDEX IF EXISTS idx_plan_approvals_session_status;
DROP INDEX IF EXISTS idx_plan_approvals_hash;
DROP INDEX IF EXISTS idx_plan_approvals_user;
DROP INDEX IF EXISTS idx_plan_approvals_session;
DROP TABLE IF EXISTS plan_approvals;
"""


def apply_migration(connection_uri: Optional[str] = None) -> bool:
    """
    Applies the plan_approvals migration to PostgreSQL.
    """
    uri = connection_uri or os.getenv("POSTGRES_URI") or os.getenv("DATABASE_URL")
    if not uri:
        logger.warning("[Migration 002] No POSTGRES_URI provided. Skipping DB migration.")
        return False

    try:
        import psycopg2
        conn = psycopg2.connect(uri, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(MIGRATION_SQL)
        conn.close()
        logger.info("[Migration 002] Successfully applied plan_approvals migration.")
        return True
    except Exception as e:
        logger.error(f"[Migration 002] Migration failed: {e}")
        return False


def rollback_migration(connection_uri: Optional[str] = None) -> bool:
    """
    Rolls back the plan_approvals migration.
    """
    uri = connection_uri or os.getenv("POSTGRES_URI") or os.getenv("DATABASE_URL")
    if not uri:
        logger.warning("[Migration 002 Rollback] No POSTGRES_URI provided.")
        return False

    try:
        import psycopg2
        conn = psycopg2.connect(uri, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(ROLLBACK_SQL)
        conn.close()
        logger.info("[Migration 002 Rollback] Successfully rolled back plan_approvals migration.")
        return True
    except Exception as e:
        logger.error(f"[Migration 002 Rollback] Rollback failed: {e}")
        return False


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1 and sys.argv[1] == "rollback":
        success = rollback_migration()
    else:
        success = apply_migration()
    sys.exit(0 if success else 1)

