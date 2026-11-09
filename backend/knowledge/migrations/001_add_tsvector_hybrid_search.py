"""
001_add_tsvector_hybrid_search.py — PostgreSQL Hybrid Full-Text Search Migration
================================================================================
Adds tsvector generated columns and GIN indexes to:
- knowledge_chunks (for knowledge base hybrid retrieval)
- execution_memories (for semantic execution memory hybrid retrieval)

Provides clean, safe upgrade and rollback procedures with zero data loss.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

MIGRATION_SQL = """
-- 1. Add tsvector column and GIN index for knowledge_chunks
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'knowledge_chunks' AND column_name = 'tsv_content'
    ) THEN
        ALTER TABLE knowledge_chunks ADD COLUMN tsv_content tsvector 
        GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_tsv 
ON knowledge_chunks USING gin(tsv_content);

-- 2. Add tsvector column and GIN index for execution_memories
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'execution_memories' AND column_name = 'tsv_content'
    ) THEN
        ALTER TABLE execution_memories ADD COLUMN tsv_content tsvector 
        GENERATED ALWAYS AS (to_tsvector('english', goal_text || ' ' || task_summary)) STORED;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_exec_memories_tsv 
ON execution_memories USING gin(tsv_content);
"""

ROLLBACK_SQL = """
DROP INDEX IF EXISTS idx_knowledge_chunks_tsv;
ALTER TABLE knowledge_chunks DROP COLUMN IF EXISTS tsv_content;

DROP INDEX IF EXISTS idx_exec_memories_tsv;
ALTER TABLE execution_memories DROP COLUMN IF EXISTS tsv_content;
"""


def apply_migration(connection_uri: Optional[str] = None) -> bool:
    """
    Applies the hybrid full-text search migration to PostgreSQL.
    """
    uri = connection_uri or os.getenv("POSTGRES_URI") or os.getenv("DATABASE_URL")
    if not uri:
        logger.warning("[Migration 001] No POSTGRES_URI provided. Skipping DB migration.")
        return False

    try:
        import psycopg2
        conn = psycopg2.connect(uri, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(MIGRATION_SQL)
        conn.close()
        logger.info("[Migration 001] Successfully applied tsvector hybrid search migration.")
        return True
    except Exception as e:
        logger.error(f"[Migration 001] Migration failed: {e}")
        return False


def rollback_migration(connection_uri: Optional[str] = None) -> bool:
    """
    Rolls back the hybrid full-text search migration.
    """
    uri = connection_uri or os.getenv("POSTGRES_URI") or os.getenv("DATABASE_URL")
    if not uri:
        logger.warning("[Migration 001 Rollback] No POSTGRES_URI provided.")
        return False

    try:
        import psycopg2
        conn = psycopg2.connect(uri, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(ROLLBACK_SQL)
        conn.close()
        logger.info("[Migration 001 Rollback] Successfully rolled back tsvector hybrid search migration.")
        return True
    except Exception as e:
        logger.error(f"[Migration 001 Rollback] Rollback failed: {e}")
        return False


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    action = sys.argv[1].lower() if len(sys.argv) > 1 else "apply"
    if action == "rollback":
        print("[Migration 001] Executing rollback...")
        success = rollback_migration()
        print(f"[Migration 001] Rollback completed: success={success}")
    elif action == "apply":
        print("[Migration 001] Applying tsvector hybrid search migration...")
        success = apply_migration()
        print(f"[Migration 001] Apply completed: success={success}")
    else:
        print(f"Unknown action: {action}. Use 'apply' or 'rollback'.")
        sys.exit(2)
    sys.exit(0 if success else 1)

