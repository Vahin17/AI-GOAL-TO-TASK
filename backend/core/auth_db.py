"""
auth_db.py — PostgreSQL & In-Memory Auth Database Persistence Layer
====================================================================
Manages tables and queries for:
- users
- sessions
- password_reset_tokens
- auth_audit_events

Supports live PostgreSQL connections (matching existing postgres_vector_store.py pattern)
and an in-memory/SQLite fallback for isolated unit testing and offline development.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.config import settings

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# PostgreSQL DDL Schemas
# ─────────────────────────────────────────────────────────────────────────────

AUTH_SCHEMA_PG_DDL = """
-- 1. Users table (password_hash is nullable to support OAuth-only accounts)
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(64) PRIMARY KEY,
    full_name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NULL,
    is_active BOOLEAN DEFAULT TRUE,
    is_verified BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMPTZ NULL
);

-- Reversible migration: Allow password_hash to be NULL if previously defined NOT NULL
DO $$
BEGIN
    ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;
EXCEPTION
    WHEN others THEN NULL;
END $$;

-- 2. Sessions table (stores only SHA-256 token hashes, NEVER raw tokens)
CREATE TABLE IF NOT EXISTS sessions (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,
    last_activity_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    revoked_at TIMESTAMPTZ NULL,
    ip_address VARCHAR(45) NULL,
    user_agent TEXT NULL
);

-- 3. Password reset tokens table (stores only SHA-256 token hashes)
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 4. Auth audit events table
CREATE TABLE IF NOT EXISTS auth_audit_events (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NULL,
    event_type VARCHAR(64) NOT NULL,
    ip_address VARCHAR(45) NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    metadata JSONB DEFAULT '{}'
);

-- 5. OAuth external provider identities table
CREATE TABLE IF NOT EXISTS oauth_accounts (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider VARCHAR(32) NOT NULL,
    provider_subject_id VARCHAR(255) NOT NULL,
    provider_email VARCHAR(255) NULL,
    provider_email_verified BOOLEAN DEFAULT FALSE,
    linked_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_oauth_provider_subject UNIQUE (provider, provider_subject_id)
);

-- 6. Transactional Outbox for asynchronous, reliable MongoDB user profile synchronization
CREATE TABLE IF NOT EXISTS outbox_events (
    id VARCHAR(64) PRIMARY KEY,
    event_type VARCHAR(64) NOT NULL,
    aggregate_id VARCHAR(64) NOT NULL,
    payload TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMPTZ NULL
);

-- 7. Plan approvals (cryptographically bound to canonical plan SHA-256 hash)
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

-- 8. Execution Evidence (Phase 3: Cryptographic Integrity & Provenance)
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

-- 9. Reusable Playbooks (Phase 3: Controlled Learning & Promotion)
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

-- Indexes for high-performance lookup & unique constraints
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower ON users (LOWER(email));
CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions (token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_password_reset_token_hash ON password_reset_tokens (token_hash);
CREATE INDEX IF NOT EXISTS idx_oauth_accounts_user_id ON oauth_accounts (user_id);
CREATE INDEX IF NOT EXISTS idx_outbox_events_status ON outbox_events (status, created_at);
CREATE INDEX IF NOT EXISTS idx_plan_approvals_session ON plan_approvals (session_id);
CREATE INDEX IF NOT EXISTS idx_plan_approvals_user ON plan_approvals (user_id);
CREATE INDEX IF NOT EXISTS idx_plan_approvals_hash ON plan_approvals (plan_hash);
CREATE INDEX IF NOT EXISTS idx_plan_approvals_session_status ON plan_approvals (session_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_session ON execution_evidence (session_id);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_user_session ON execution_evidence (user_id, session_id);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_task ON execution_evidence (session_id, task_id);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_hash ON execution_evidence (artifact_hash);
CREATE INDEX IF NOT EXISTS idx_playbooks_user_category ON playbooks (user_id, workflow_category);
CREATE INDEX IF NOT EXISTS idx_playbooks_status ON playbooks (status);
CREATE INDEX IF NOT EXISTS idx_playbooks_provenance_hash ON playbooks (provenance_plan_hash);
"""

# SQLite DDL for local in-memory fallback & unit testing
AUTH_SCHEMA_SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NULL,
    is_active INTEGER DEFAULT 1,
    is_verified INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    last_activity_at TEXT DEFAULT CURRENT_TIMESTAMP,
    revoked_at TEXT NULL,
    ip_address TEXT NULL,
    user_agent TEXT NULL
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at TEXT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS auth_audit_events (
    id TEXT PRIMARY KEY,
    user_id TEXT NULL,
    event_type TEXT NOT NULL,
    ip_address TEXT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS oauth_accounts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_subject_id TEXT NOT NULL,
    provider_email TEXT NULL,
    provider_email_verified INTEGER DEFAULT 0,
    linked_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (provider, provider_subject_id)
);

CREATE TABLE IF NOT EXISTS outbox_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    processed_at TEXT NULL
);

CREATE TABLE IF NOT EXISTS plan_approvals (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_hash TEXT NOT NULL,
    plan_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'approved',
    task_id TEXT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    revoked_at TEXT NULL,
    metadata TEXT DEFAULT '{}'
);

-- 8. Execution Evidence (SQLite fallback)
CREATE TABLE IF NOT EXISTS execution_evidence (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    task_id TEXT NOT NULL,
    task_name TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    captured_at TEXT DEFAULT CURRENT_TIMESTAMP,
    verification_status TEXT NOT NULL DEFAULT 'inconclusive',
    verification_reason TEXT NULL,
    confidence_score REAL NOT NULL DEFAULT 0.0,
    metadata TEXT DEFAULT '{}',
    content_payload TEXT NULL
);

-- 9. Reusable Playbooks (SQLite fallback)
CREATE TABLE IF NOT EXISTS playbooks (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workflow_category TEXT NOT NULL,
    goal_pattern TEXT NOT NULL,
    preconditions TEXT DEFAULT '[]',
    execution_steps TEXT NOT NULL DEFAULT '[]',
    required_tools TEXT NOT NULL DEFAULT '[]',
    expected_outcomes TEXT NOT NULL DEFAULT '[]',
    supporting_evidence_ids TEXT DEFAULT '[]',
    provenance_session_id TEXT NOT NULL,
    provenance_plan_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate',
    confidence_score REAL NOT NULL DEFAULT 0.5,
    success_count INTEGER NOT NULL DEFAULT 1,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_used_at TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NULL,
    metadata TEXT DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower ON users (email COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions (token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_password_reset_token_hash ON password_reset_tokens (token_hash);
CREATE INDEX IF NOT EXISTS idx_oauth_accounts_user_id ON oauth_accounts (user_id);
CREATE INDEX IF NOT EXISTS idx_outbox_events_status ON outbox_events (status, created_at);
CREATE INDEX IF NOT EXISTS idx_plan_approvals_session ON plan_approvals (session_id);
CREATE INDEX IF NOT EXISTS idx_plan_approvals_user ON plan_approvals (user_id);
CREATE INDEX IF NOT EXISTS idx_plan_approvals_hash ON plan_approvals (plan_hash);
CREATE INDEX IF NOT EXISTS idx_plan_approvals_session_status ON plan_approvals (session_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_session ON execution_evidence (session_id);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_user_session ON execution_evidence (user_id, session_id);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_task ON execution_evidence (session_id, task_id);
CREATE INDEX IF NOT EXISTS idx_execution_evidence_hash ON execution_evidence (artifact_hash);
CREATE INDEX IF NOT EXISTS idx_playbooks_user_category ON playbooks (user_id, workflow_category);
CREATE INDEX IF NOT EXISTS idx_playbooks_status ON playbooks (status);
CREATE INDEX IF NOT EXISTS idx_playbooks_provenance_hash ON playbooks (provenance_plan_hash);
"""


def hash_token(token: str) -> str:
    """Return SHA-256 hex digest of opaque token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthDatabase:
    """
    Authoritative authentication persistence manager.
    
    PostgreSQL is the authoritative authentication database for all production and
    development configurations. Silent fallback to SQLite is strictly forbidden.
    
    SQLite is permitted solely when explicitly requested for isolated unit tests
    via force_sqlite=True or AUTH_DB_BACKEND='sqlite'.
    """

    _instance: Optional["AuthDatabase"] = None

    def __init__(self, connection_uri: Optional[str] = None, force_sqlite: bool = False):
        self.connection_uri = connection_uri or settings.POSTGRES_URI or os.getenv("POSTGRES_URI")
        self.force_sqlite = force_sqlite or (getattr(settings, "AUTH_DB_BACKEND", "postgres").lower() == "sqlite")
        self._is_pg = False
        self._pg_conn = None
        self._sqlite_conn: Optional[sqlite3.Connection] = None

        if self.force_sqlite:
            self._init_sqlite()
        else:
            self._connect_pg()

    @classmethod
    def get_instance(cls, force_sqlite: bool = False) -> "AuthDatabase":
        if cls._instance is None or force_sqlite:
            cls._instance = cls(force_sqlite=force_sqlite)
        return cls._instance

    @classmethod
    def reset_instance(cls, force_sqlite: bool = False) -> "AuthDatabase":
        cls._instance = cls(force_sqlite=force_sqlite)
        return cls._instance

    def _connect_pg(self) -> None:
        """
        Connects to authoritative PostgreSQL database.
        Raises RuntimeError if connection fails — silent fallback is strictly prevented.
        """
        try:
            import psycopg2
            self._pg_conn = psycopg2.connect(self.connection_uri, connect_timeout=5)
            self._pg_conn.autocommit = True
            with self._pg_conn.cursor() as cur:
                cur.execute(AUTH_SCHEMA_PG_DDL)
            self._is_pg = True
            logger.info("[AuthDatabase] Connected to authoritative PostgreSQL database with auth tables initialized.")
        except Exception as e:
            self._is_pg = False
            self._pg_conn = None
            masked_uri = self.connection_uri or ""
            if "@" in masked_uri:
                parts = masked_uri.split("@")
                prefix = parts[0].split("://")[0]
                masked_uri = f"{prefix}://****:****@{parts[1]}"
            error_msg = (
                f"[AuthDatabase] Failed to connect to authoritative PostgreSQL database at {masked_uri}: {e}. "
                "Silent fallback to SQLite is disabled in development and production to prevent split database state. "
                "Ensure PostgreSQL is running, or set AUTH_DB_BACKEND='sqlite' / force_sqlite=True ONLY for isolated test runs."
            )
            logger.critical(error_msg)
            raise RuntimeError(error_msg) from e

    def _init_sqlite(self) -> None:
        """Isolated in-memory SQLite store strictly for automated testing."""
        self._sqlite_conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._sqlite_conn.row_factory = sqlite3.Row
        self._sqlite_conn.execute("PRAGMA foreign_keys = ON;")
        self._sqlite_conn.executescript(AUTH_SCHEMA_SQLITE_DDL)
        self._sqlite_conn.commit()
        logger.info("[AuthDatabase] Initialized explicitly configured isolated SQLite auth store for testing.")

    def check_health(self) -> Dict[str, Any]:
        """
        Actively probes database health and measures round-trip query latency.
        """
        import time
        start = time.time()
        try:
            if self._is_pg and self._pg_conn:
                with self._pg_conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                    cur.fetchone()
                latency_ms = round((time.time() - start) * 1000, 2)
                return {"status": "healthy", "backend": "postgresql", "latency_ms": latency_ms}
            elif self._sqlite_conn:
                cur = self._sqlite_conn.cursor()
                cur.execute("SELECT 1;")
                cur.fetchone()
                latency_ms = round((time.time() - start) * 1000, 2)
                return {"status": "healthy", "backend": "sqlite", "latency_ms": latency_ms}
            return {"status": "unhealthy", "backend": "none", "error": "No active database connection"}
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}

    # ─────────────────────────────────────────────────────────────────────────
    # User Operations
    # ─────────────────────────────────────────────────────────────────────────

    def create_user(
        self,
        full_name: str,
        email: str,
        password_hash: Optional[str] = None,
        is_active: bool = True,
        is_verified: bool = False,
    ) -> Dict[str, Any]:
        """Create a new user with normalized email. Raises ValueError if email exists."""
        email_clean = email.strip().lower()
        user_id = f"usr_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        user_record: Optional[Dict[str, Any]] = None

        if self._is_pg and self._pg_conn:
            import psycopg2
            try:
                with self._pg_conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO users (id, full_name, email, password_hash, is_active, is_verified, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                        RETURNING id, full_name, email, password_hash, is_active, is_verified, created_at, updated_at, last_login_at;
                        """,
                        (user_id, full_name.strip(), email_clean, password_hash, is_active, is_verified),
                    )
                    row = cur.fetchone()
                    user_record = {
                        "id": row[0],
                        "full_name": row[1],
                        "email": row[2],
                        "password_hash": row[3],
                        "is_active": bool(row[4]),
                        "is_verified": bool(row[5]),
                        "created_at": str(row[6]),
                        "updated_at": str(row[7]),
                        "last_login_at": str(row[8]) if row[8] else None,
                    }
            except psycopg2.IntegrityError:
                raise ValueError(f"Email '{email_clean}' is already registered.")
        else:
            try:
                cur = self._sqlite_conn.cursor()
                cur.execute(
                    """
                    INSERT INTO users (id, full_name, email, password_hash, is_active, is_verified, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, full_name.strip(), email_clean, password_hash, 1 if is_active else 0, 1 if is_verified else 0, now, now),
                )
                self._sqlite_conn.commit()
                user_record = self.get_user_by_id(user_id)
            except sqlite3.IntegrityError:
                raise ValueError(f"Email '{email_clean}' is already registered.")

        if user_record:
            # Transactional Outbox Event for reliable MongoDB synchronization
            self.create_outbox_event(
                event_type="user_registered",
                aggregate_id=user_record["id"],
                payload={
                    "user_id": user_record["id"],
                    "email": user_record["email"],
                    "full_name": user_record["full_name"],
                    "is_active": user_record["is_active"],
                    "auth_provider": "password" if password_hash else "oauth",
                },
            )

        return user_record

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Retrieve user by normalized email."""
        email_clean = email.strip().lower()
        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT id, full_name, email, password_hash, is_active, is_verified, created_at, updated_at, last_login_at FROM users WHERE LOWER(email) = %s",
                    (email_clean,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0],
                    "full_name": row[1],
                    "email": row[2],
                    "password_hash": row[3],
                    "is_active": bool(row[4]),
                    "is_verified": bool(row[5]),
                    "created_at": str(row[6]),
                    "updated_at": str(row[7]),
                    "last_login_at": str(row[8]) if row[8] else None,
                }
        else:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                "SELECT id, full_name, email, password_hash, is_active, is_verified, created_at, updated_at, last_login_at FROM users WHERE LOWER(email) = ?",
                (email_clean,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "full_name": row["full_name"],
                "email": row["email"],
                "password_hash": row["password_hash"],
                "is_active": bool(row["is_active"]),
                "is_verified": bool(row["is_verified"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "last_login_at": str(row["last_login_at"]) if row["last_login_at"] else None,
            }

    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve user by UUID."""
        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT id, full_name, email, password_hash, is_active, is_verified, created_at, updated_at, last_login_at FROM users WHERE id = %s",
                    (user_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0],
                    "full_name": row[1],
                    "email": row[2],
                    "password_hash": row[3],
                    "is_active": bool(row[4]),
                    "is_verified": bool(row[5]),
                    "created_at": str(row[6]),
                    "updated_at": str(row[7]),
                    "last_login_at": str(row[8]) if row[8] else None,
                }
        else:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                "SELECT id, full_name, email, password_hash, is_active, is_verified, created_at, updated_at, last_login_at FROM users WHERE id = ?",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "full_name": row["full_name"],
                "email": row["email"],
                "password_hash": row["password_hash"],
                "is_active": bool(row["is_active"]),
                "is_verified": bool(row["is_verified"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "last_login_at": str(row["last_login_at"]) if row["last_login_at"] else None,
            }

    def update_last_login(self, user_id: str) -> None:
        """Update last_login_at timestamp for user."""
        now = datetime.now(timezone.utc).isoformat()
        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute("UPDATE users SET last_login_at = NOW(), updated_at = NOW() WHERE id = %s", (user_id,))
        else:
            cur = self._sqlite_conn.cursor()
            cur.execute("UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?", (now, now, user_id))
            self._sqlite_conn.commit()

    def update_password(self, user_id: str, new_password_hash: str) -> None:
        """Update user password hash and updated_at timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute("UPDATE users SET password_hash = %s, updated_at = NOW() WHERE id = %s", (new_password_hash, user_id))
        else:
            cur = self._sqlite_conn.cursor()
            cur.execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?", (new_password_hash, now, user_id))
            self._sqlite_conn.commit()

    # ─────────────────────────────────────────────────────────────────────────
    # Session Operations (Opaque Token + Hashed DB Storage)
    # ─────────────────────────────────────────────────────────────────────────

    def create_session(
        self,
        user_id: str,
        token: str,
        duration_days: int = 7,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Store hashed session token in database.
        NEVER stores raw token!
        """
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        t_hash = hash_token(token)
        now_dt = datetime.now(timezone.utc)
        expires_dt = now_dt + timedelta(days=duration_days)
        now_str = now_dt.isoformat()
        expires_str = expires_dt.isoformat()

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sessions (id, user_id, token_hash, created_at, expires_at, last_activity_at, ip_address, user_agent)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, user_id, token_hash, created_at, expires_at, last_activity_at, revoked_at, ip_address, user_agent;
                    """,
                    (session_id, user_id, t_hash, now_dt, expires_dt, now_dt, ip_address, user_agent),
                )
                row = cur.fetchone()
                return {
                    "id": row[0],
                    "user_id": row[1],
                    "token_hash": row[2],
                    "created_at": str(row[3]),
                    "expires_at": str(row[4]),
                    "last_activity_at": str(row[5]),
                    "revoked_at": str(row[6]) if row[6] else None,
                    "ip_address": row[7],
                    "user_agent": row[8],
                }
        else:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                """
                INSERT INTO sessions (id, user_id, token_hash, created_at, expires_at, last_activity_at, ip_address, user_agent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, user_id, t_hash, now_str, expires_str, now_str, ip_address, user_agent),
            )
            self._sqlite_conn.commit()
            return {
                "id": session_id,
                "user_id": user_id,
                "token_hash": t_hash,
                "created_at": now_str,
                "expires_at": expires_str,
                "last_activity_at": now_str,
                "revoked_at": None,
                "ip_address": ip_address,
                "user_agent": user_agent,
            }

    def get_session_by_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Validate token by hashing and querying session table."""
        t_hash = hash_token(token)
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_id, token_hash, created_at, expires_at, last_activity_at, revoked_at, ip_address, user_agent
                    FROM sessions
                    WHERE token_hash = %s AND revoked_at IS NULL AND expires_at > NOW();
                    """,
                    (t_hash,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                # update last activity
                cur.execute("UPDATE sessions SET last_activity_at = NOW() WHERE id = %s", (row[0],))
                return {
                    "id": row[0],
                    "user_id": row[1],
                    "token_hash": row[2],
                    "created_at": str(row[3]),
                    "expires_at": str(row[4]),
                    "last_activity_at": str(row[5]),
                    "revoked_at": str(row[6]) if row[6] else None,
                    "ip_address": row[7],
                    "user_agent": row[8],
                }
        else:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                """
                SELECT id, user_id, token_hash, created_at, expires_at, last_activity_at, revoked_at, ip_address, user_agent
                FROM sessions
                WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > ?;
                """,
                (t_hash, now_str),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur.execute("UPDATE sessions SET last_activity_at = ? WHERE id = ?", (now_str, row["id"]))
            self._sqlite_conn.commit()
            return {
                "id": row["id"],
                "user_id": row["user_id"],
                "token_hash": row["token_hash"],
                "created_at": str(row["created_at"]),
                "expires_at": str(row["expires_at"]),
                "last_activity_at": str(row["last_activity_at"]),
                "revoked_at": str(row["revoked_at"]) if row["revoked_at"] else None,
                "ip_address": row["ip_address"],
                "user_agent": row["user_agent"],
            }

    def revoke_session(self, token: str) -> bool:
        """Revoke a session by token hash."""
        t_hash = hash_token(token)
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute("UPDATE sessions SET revoked_at = NOW() WHERE token_hash = %s AND revoked_at IS NULL", (t_hash,))
                return cur.rowcount > 0
        else:
            cur = self._sqlite_conn.cursor()
            cur.execute("UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL", (now_str, t_hash))
            self._sqlite_conn.commit()
            return cur.rowcount > 0

    def revoke_all_user_sessions(self, user_id: str, except_token: Optional[str] = None) -> int:
        """Revoke all sessions for a user (e.g. on password reset/change)."""
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()
        except_hash = hash_token(except_token) if except_token else None

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                if except_hash:
                    cur.execute(
                        "UPDATE sessions SET revoked_at = NOW() WHERE user_id = %s AND token_hash != %s AND revoked_at IS NULL",
                        (user_id, except_hash),
                    )
                else:
                    cur.execute("UPDATE sessions SET revoked_at = NOW() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
                return cur.rowcount
        else:
            cur = self._sqlite_conn.cursor()
            if except_hash:
                cur.execute(
                    "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND token_hash != ? AND revoked_at IS NULL",
                    (now_str, user_id, except_hash),
                )
            else:
                cur.execute("UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (now_str, user_id))
            self._sqlite_conn.commit()
            return cur.rowcount

    # ─────────────────────────────────────────────────────────────────────────
    # Password Reset Token Operations
    # ─────────────────────────────────────────────────────────────────────────

    def create_password_reset_token(self, user_id: str, token: str, expiry_minutes: int = 15) -> str:
        """Store hashed password reset token."""
        t_id = f"prt_{uuid.uuid4().hex[:12]}"
        t_hash = hash_token(token)
        now_dt = datetime.now(timezone.utc)
        expires_dt = now_dt + timedelta(minutes=expiry_minutes)
        now_str = now_dt.isoformat()
        expires_str = expires_dt.isoformat()

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO password_reset_tokens (id, user_id, token_hash, created_at, expires_at) VALUES (%s, %s, %s, %s, %s)",
                    (t_id, user_id, t_hash, now_dt, expires_dt),
                )
        else:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                "INSERT INTO password_reset_tokens (id, user_id, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (t_id, user_id, t_hash, now_str, expires_str),
            )
            self._sqlite_conn.commit()
        return t_id

    def get_and_validate_reset_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Validate reset token: must exist, not be used, and not be expired."""
        t_hash = hash_token(token)
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_id, token_hash, expires_at, used_at, created_at
                    FROM password_reset_tokens
                    WHERE token_hash = %s AND used_at IS NULL AND expires_at > NOW();
                    """,
                    (t_hash,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0],
                    "user_id": row[1],
                    "token_hash": row[2],
                    "expires_at": str(row[3]),
                    "used_at": str(row[4]) if row[4] else None,
                    "created_at": str(row[5]),
                }
        else:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                """
                SELECT id, user_id, token_hash, expires_at, used_at, created_at
                FROM password_reset_tokens
                WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?;
                """,
                (t_hash, now_str),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "user_id": row["user_id"],
                "token_hash": row["token_hash"],
                "expires_at": str(row["expires_at"]),
                "used_at": str(row["used_at"]) if row["used_at"] else None,
                "created_at": str(row["created_at"]),
            }

    def mark_reset_token_used(self, token: str) -> bool:
        """Mark reset token as used (single-use enforcement)."""
        t_hash = hash_token(token)
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute("UPDATE password_reset_tokens SET used_at = NOW() WHERE token_hash = %s AND used_at IS NULL", (t_hash,))
                return cur.rowcount > 0
        else:
            cur = self._sqlite_conn.cursor()
            cur.execute("UPDATE password_reset_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL", (now_str, t_hash))
            self._sqlite_conn.commit()
            return cur.rowcount > 0

    # ─────────────────────────────────────────────────────────────────────────
    # Audit Event Operations
    # ─────────────────────────────────────────────────────────────────────────

    def record_audit_event(
        self,
        event_type: str,
        user_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log authentication audit event safely without logging sensitive credentials."""
        evt_id = f"evt_{uuid.uuid4().hex[:12]}"
        meta_str = json.dumps(metadata or {})
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()

        try:
            if self._is_pg and self._pg_conn:
                with self._pg_conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO auth_audit_events (id, user_id, event_type, ip_address, created_at, metadata) VALUES (%s, %s, %s, %s, %s, %s)",
                        (evt_id, user_id, event_type, ip_address, now_dt, meta_str),
                    )
            elif self._sqlite_conn:
                cur = self._sqlite_conn.cursor()
                cur.execute(
                    "INSERT INTO auth_audit_events (id, user_id, event_type, ip_address, created_at, metadata) VALUES (?, ?, ?, ?, ?, ?)",
                    (evt_id, user_id, event_type, ip_address, now_str, meta_str),
                )
                self._sqlite_conn.commit()
        except Exception as e:
            logger.warning(f"[AuthDatabase] Failed to log audit event ({event_type}): {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # OAuth Identity Operations
    # ─────────────────────────────────────────────────────────────────────────

    def create_oauth_account(
        self,
        user_id: str,
        provider: str,
        provider_subject_id: str,
        provider_email: Optional[str] = None,
        provider_email_verified: bool = False,
    ) -> Dict[str, Any]:
        """
        Safely link an external OAuth identity (Google, Facebook) to an ARDO user.
        Enforces unique constraint on (provider, provider_subject_id).
        """
        provider_clean = provider.strip().lower()
        sub_id_clean = str(provider_subject_id).strip()
        email_clean = provider_email.strip().lower() if provider_email else None
        oac_id = f"oac_{uuid.uuid4().hex[:12]}"
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()

        if self._is_pg and self._pg_conn:
            import psycopg2
            try:
                with self._pg_conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO oauth_accounts (id, user_id, provider, provider_subject_id, provider_email, provider_email_verified, linked_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                        RETURNING id, user_id, provider, provider_subject_id, provider_email, provider_email_verified, linked_at, updated_at;
                        """,
                        (oac_id, user_id, provider_clean, sub_id_clean, email_clean, provider_email_verified),
                    )
                    row = cur.fetchone()
                    res = {
                        "id": row[0],
                        "user_id": row[1],
                        "provider": row[2],
                        "provider_subject_id": row[3],
                        "provider_email": row[4],
                        "provider_email_verified": bool(row[5]),
                        "linked_at": str(row[6]),
                        "updated_at": str(row[7]),
                    }
            except psycopg2.IntegrityError:
                raise ValueError(f"The {provider} account is already linked to another ARDO user.")
        else:
            try:
                cur = self._sqlite_conn.cursor()
                cur.execute(
                    """
                    INSERT INTO oauth_accounts (id, user_id, provider, provider_subject_id, provider_email, provider_email_verified, linked_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (oac_id, user_id, provider_clean, sub_id_clean, email_clean, 1 if provider_email_verified else 0, now_str, now_str),
                )
                self._sqlite_conn.commit()
                res = {
                    "id": oac_id,
                    "user_id": user_id,
                    "provider": provider_clean,
                    "provider_subject_id": sub_id_clean,
                    "provider_email": email_clean,
                    "provider_email_verified": provider_email_verified,
                    "linked_at": now_str,
                    "updated_at": now_str,
                }
            except sqlite3.IntegrityError:
                raise ValueError(f"The {provider} account is already linked to another ARDO user.")

        # Atomic Outbox Event for MongoDB sync
        self.create_outbox_event(
            event_type="oauth_account_linked",
            aggregate_id=user_id,
            payload={
                "user_id": user_id,
                "provider": provider_clean,
                "provider_email": email_clean,
                "provider_email_verified": provider_email_verified,
            },
        )
        return res

    def get_oauth_account(self, provider: str, provider_subject_id: str) -> Optional[Dict[str, Any]]:
        """Find linked OAuth account by provider and external subject/user ID."""
        provider_clean = provider.strip().lower()
        sub_id_clean = str(provider_subject_id).strip()

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_id, provider, provider_subject_id, provider_email, provider_email_verified, linked_at, updated_at
                    FROM oauth_accounts
                    WHERE provider = %s AND provider_subject_id = %s;
                    """,
                    (provider_clean, sub_id_clean),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0],
                    "user_id": row[1],
                    "provider": row[2],
                    "provider_subject_id": row[3],
                    "provider_email": row[4],
                    "provider_email_verified": bool(row[5]),
                    "linked_at": str(row[6]),
                    "updated_at": str(row[7]),
                }
        else:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                """
                SELECT id, user_id, provider, provider_subject_id, provider_email, provider_email_verified, linked_at, updated_at
                FROM oauth_accounts
                WHERE provider = ? AND provider_subject_id = ?;
                """,
                (provider_clean, sub_id_clean),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "user_id": row["user_id"],
                "provider": row["provider"],
                "provider_subject_id": row["provider_subject_id"],
                "provider_email": row["provider_email"],
                "provider_email_verified": bool(row["provider_email_verified"]),
                "linked_at": str(row["linked_at"]),
                "updated_at": str(row["updated_at"]),
            }

    def get_user_oauth_accounts(self, user_id: str) -> List[Dict[str, Any]]:
        """List all external OAuth providers linked to an ARDO user."""
        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_id, provider, provider_subject_id, provider_email, provider_email_verified, linked_at, updated_at
                    FROM oauth_accounts
                    WHERE user_id = %s
                    ORDER BY linked_at ASC;
                    """,
                    (user_id,),
                )
                rows = cur.fetchall()
                return [
                    {
                        "id": r[0],
                        "user_id": r[1],
                        "provider": r[2],
                        "provider_subject_id": r[3],
                        "provider_email": r[4],
                        "provider_email_verified": bool(r[5]),
                        "linked_at": str(r[6]),
                        "updated_at": str(r[7]),
                    }
                    for r in rows
                ]
        else:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                """
                SELECT id, user_id, provider, provider_subject_id, provider_email, provider_email_verified, linked_at, updated_at
                FROM oauth_accounts
                WHERE user_id = ?
                ORDER BY linked_at ASC;
                """,
                (user_id,),
            )
            rows = cur.fetchall()
            return [
                {
                    "id": r["id"],
                    "user_id": r["user_id"],
                    "provider": r["provider"],
                    "provider_subject_id": r["provider_subject_id"],
                    "provider_email": r["provider_email"],
                    "provider_email_verified": bool(r["provider_email_verified"]),
                    "linked_at": str(r["linked_at"]),
                    "updated_at": str(r["updated_at"]),
                }
                for r in rows
            ]

    def delete_oauth_account(self, user_id: str, provider: str) -> bool:
        """Unlink an OAuth account from an ARDO user."""
        provider_clean = provider.strip().lower()
        deleted = False

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM oauth_accounts WHERE user_id = %s AND provider = %s",
                    (user_id, provider_clean),
                )
                deleted = cur.rowcount > 0
        else:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                "DELETE FROM oauth_accounts WHERE user_id = ? AND provider = ?",
                (user_id, provider_clean),
            )
            self._sqlite_conn.commit()
            deleted = cur.rowcount > 0

        if deleted:
            self.create_outbox_event(
                event_type="oauth_account_unlinked",
                aggregate_id=user_id,
                payload={"user_id": user_id, "provider": provider_clean},
            )
        return deleted

    def count_user_auth_methods(self, user_id: str) -> Dict[str, Any]:
        """
        Check all active authentication methods available to a user.
        Used to prevent a user from removing their last usable login method.
        """
        user = self.get_user_by_id(user_id)
        has_password = bool(user and user.get("password_hash") and len(user["password_hash"].strip()) > 0)
        oauth_accounts = self.get_user_oauth_accounts(user_id)

        methods: List[str] = []
        if has_password:
            methods.append("password")
        for oac in oauth_accounts:
            methods.append(oac["provider"])

        return {
            "user_id": user_id,
            "has_password": has_password,
            "oauth_accounts": oauth_accounts,
            "total_methods": len(methods),
            "providers": methods,
        }

    def update_user_profile(self, user_id: str, full_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Update authoritative user identity profile fields in PostgreSQL."""
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()

        if full_name is not None:
            if self._is_pg and self._pg_conn:
                with self._pg_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET full_name = %s, updated_at = NOW() WHERE id = %s",
                        (full_name.strip(), user_id),
                    )
            else:
                cur = self._sqlite_conn.cursor()
                cur.execute(
                    "UPDATE users SET full_name = ?, updated_at = ? WHERE id = ?",
                    (full_name.strip(), now_str, user_id),
                )
                self._sqlite_conn.commit()

        updated_user = self.get_user_by_id(user_id)
        if updated_user:
            self.create_outbox_event(
                event_type="user_profile_updated",
                aggregate_id=user_id,
                payload={
                    "user_id": user_id,
                    "full_name": updated_user["full_name"],
                    "email": updated_user["email"],
                },
            )
        return updated_user

    def update_password(self, user_id: str, password_hash: str) -> bool:
        """Update user password hash and record outbox sync event."""
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()
        updated = False

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute("UPDATE users SET password_hash = %s, updated_at = NOW() WHERE id = %s", (password_hash, user_id))
                updated = cur.rowcount > 0
        else:
            cur = self._sqlite_conn.cursor()
            cur.execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?", (password_hash, now_str, user_id))
            self._sqlite_conn.commit()
            updated = cur.rowcount > 0

        if updated:
            self.create_outbox_event(
                event_type="password_updated",
                aggregate_id=user_id,
                payload={"user_id": user_id, "has_password": True},
            )
        return updated

    # ─────────────────────────────────────────────────────────────────────────
    # Transactional Outbox Operations (PostgreSQL -> MongoDB Sync)
    # ─────────────────────────────────────────────────────────────────────────

    def create_outbox_event(self, event_type: str, aggregate_id: str, payload: Dict[str, Any]) -> str:
        """
        Atomically persist an outbox event.
        Guarantees reliable, asynchronous dispatch to MongoDB without dual-write risks.
        """
        obx_id = f"obx_{uuid.uuid4().hex[:12]}"
        meta_str = json.dumps(payload)
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()

        try:
            if self._is_pg and self._pg_conn:
                with self._pg_conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO outbox_events (id, event_type, aggregate_id, payload, status, attempts, created_at)
                        VALUES (%s, %s, %s, %s, 'pending', 0, NOW())
                        """,
                        (obx_id, event_type, aggregate_id, meta_str),
                    )
            elif self._sqlite_conn:
                cur = self._sqlite_conn.cursor()
                cur.execute(
                    """
                    INSERT INTO outbox_events (id, event_type, aggregate_id, payload, status, attempts, created_at)
                    VALUES (?, ?, ?, ?, 'pending', 0, ?)
                    """,
                    (obx_id, event_type, aggregate_id, meta_str, now_str),
                )
                self._sqlite_conn.commit()
        except Exception as e:
            logger.error(f"[AuthDatabase] Failed to insert outbox event ({event_type}): {e}")

        return obx_id

    def fetch_pending_outbox_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Fetch unhandled outbox events ordered by creation timestamp."""
        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, event_type, aggregate_id, payload, status, attempts, last_error, created_at
                    FROM outbox_events
                    WHERE status = 'pending' AND attempts < 5
                    ORDER BY created_at ASC
                    LIMIT %s;
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
                return [
                    {
                        "id": r[0],
                        "event_type": r[1],
                        "aggregate_id": r[2],
                        "payload": json.loads(r[3]),
                        "status": r[4],
                        "attempts": r[5],
                        "last_error": r[6],
                        "created_at": str(r[7]),
                    }
                    for r in rows
                ]
        elif self._sqlite_conn:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                """
                SELECT id, event_type, aggregate_id, payload, status, attempts, last_error, created_at
                FROM outbox_events
                WHERE status = 'pending' AND attempts < 5
                ORDER BY created_at ASC
                LIMIT ?;
                """,
                (limit,),
            )
            rows = cur.fetchall()
            return [
                {
                    "id": r["id"],
                    "event_type": r["event_type"],
                    "aggregate_id": r["aggregate_id"],
                    "payload": json.loads(r["payload"]),
                    "status": r["status"],
                    "attempts": r["attempts"],
                    "last_error": r["last_error"],
                    "created_at": str(r["created_at"]),
                }
                for r in rows
            ]
        return []

    def mark_outbox_event_processed(self, event_id: str) -> bool:
        """Mark outbox event as successfully synchronized to MongoDB."""
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    "UPDATE outbox_events SET status = 'processed', processed_at = NOW() WHERE id = %s",
                    (event_id,),
                )
                return cur.rowcount > 0
        elif self._sqlite_conn:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                "UPDATE outbox_events SET status = 'processed', processed_at = ? WHERE id = ?",
                (now_str, event_id),
            )
            self._sqlite_conn.commit()
            return cur.rowcount > 0
        return False

    def mark_outbox_event_failed(self, event_id: str, error_message: str, max_attempts: int = 5) -> bool:
        """Record synchronization failure with error message and retry count increment."""
        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE outbox_events
                    SET attempts = attempts + 1,
                        last_error = %s,
                        status = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE 'pending' END
                    WHERE id = %s;
                    """,
                    (error_message[:500], max_attempts, event_id),
                )
                return cur.rowcount > 0
        elif self._sqlite_conn:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                """
                UPDATE outbox_events
                SET attempts = attempts + 1,
                    last_error = ?,
                    status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'pending' END
                WHERE id = ?;
                """,
                (error_message[:500], max_attempts, event_id),
            )
            self._sqlite_conn.commit()
            return cur.rowcount > 0
        return False

    def retry_failed_outbox_events(self, max_age_hours: int = 24) -> int:
        """
        Recovers outbox events previously marked 'failed' by resetting status to 'pending'
        and clearing attempts. Enables automatic recovery after database / network restores.
        """
        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE outbox_events
                    SET status = 'pending', attempts = 0
                    WHERE status = 'failed'
                      AND created_at >= NOW() - INTERVAL '%s hours';
                    """,
                    (max_age_hours,),
                )
                return cur.rowcount
        elif self._sqlite_conn:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                """
                UPDATE outbox_events
                SET status = 'pending', attempts = 0
                WHERE status = 'failed';
                """
            )
            self._sqlite_conn.commit()
            return cur.rowcount
        return 0

    # ─────────────────────────────────────────────────────────────────────────
    # Phase 2: Plan Approval Cryptographic Binding
    # ─────────────────────────────────────────────────────────────────────────

    def record_plan_approval(
        self,
        session_id: str,
        user_id: str,
        plan_hash: str,
        plan_version: int = 1,
        status: str = "approved",
        task_id: Optional[str] = None,
        expires_in_seconds: int = 900,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Cryptographically bind an operator approval to an authoritative plan hash,
        session, user, and expiration timestamp.
        """
        approval_id = f"appr_{uuid.uuid4().hex[:12]}"
        now_dt = datetime.now(timezone.utc)
        expires_dt = now_dt + timedelta(seconds=expires_in_seconds)
        now_str = now_dt.isoformat()
        expires_str = expires_dt.isoformat()
        meta = metadata or {}
        meta_json = json.dumps(meta)

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO plan_approvals (
                        id, session_id, user_id, plan_hash, plan_version, status,
                        task_id, created_at, expires_at, metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, session_id, user_id, plan_hash, plan_version, status,
                              task_id, created_at, expires_at, revoked_at, metadata;
                    """,
                    (
                        approval_id, session_id, user_id, plan_hash, plan_version,
                        status, task_id, now_dt, expires_dt, meta_json
                    ),
                )
                row = cur.fetchone()
                return {
                    "id": row[0],
                    "session_id": row[1],
                    "user_id": row[2],
                    "plan_hash": row[3],
                    "plan_version": row[4],
                    "status": row[5],
                    "task_id": row[6],
                    "created_at": str(row[7]),
                    "expires_at": str(row[8]),
                    "revoked_at": str(row[9]) if row[9] else None,
                    "metadata": row[10] if isinstance(row[10], dict) else json.loads(row[10] or "{}"),
                }
        elif self._sqlite_conn:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                """
                INSERT INTO plan_approvals (
                    id, session_id, user_id, plan_hash, plan_version, status,
                    task_id, created_at, expires_at, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    approval_id, session_id, user_id, plan_hash, plan_version,
                    status, task_id, now_str, expires_str, meta_json
                ),
            )
            self._sqlite_conn.commit()
            return {
                "id": approval_id,
                "session_id": session_id,
                "user_id": user_id,
                "plan_hash": plan_hash,
                "plan_version": plan_version,
                "status": status,
                "task_id": task_id,
                "created_at": now_str,
                "expires_at": expires_str,
                "revoked_at": None,
                "metadata": meta,
            }
        raise RuntimeError("No active database connection in AuthDatabase.")

    def get_plan_approval(
        self,
        session_id: str,
        plan_hash: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve most recent approval record for session."""
        if self._is_pg and self._pg_conn:
            query = "SELECT id, session_id, user_id, plan_hash, plan_version, status, task_id, created_at, expires_at, revoked_at, metadata FROM plan_approvals WHERE session_id = %s"
            params: List[Any] = [session_id]
            if plan_hash:
                query += " AND plan_hash = %s"
                params.append(plan_hash)
            if task_id:
                query += " AND (task_id = %s OR task_id IS NULL)"
                params.append(task_id)
            query += " ORDER BY created_at DESC LIMIT 1"

            with self._pg_conn.cursor() as cur:
                cur.execute(query, tuple(params))
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0],
                    "session_id": row[1],
                    "user_id": row[2],
                    "plan_hash": row[3],
                    "plan_version": row[4],
                    "status": row[5],
                    "task_id": row[6],
                    "created_at": str(row[7]),
                    "expires_at": str(row[8]),
                    "revoked_at": str(row[9]) if row[9] else None,
                    "metadata": row[10] if isinstance(row[10], dict) else json.loads(row[10] or "{}"),
                }
        elif self._sqlite_conn:
            query = "SELECT id, session_id, user_id, plan_hash, plan_version, status, task_id, created_at, expires_at, revoked_at, metadata FROM plan_approvals WHERE session_id = ?"
            params = [session_id]
            if plan_hash:
                query += " AND plan_hash = ?"
                params.append(plan_hash)
            if task_id:
                query += " AND (task_id = ? OR task_id IS NULL)"
                params.append(task_id)
            query += " ORDER BY created_at DESC LIMIT 1"

            cur = self._sqlite_conn.cursor()
            cur.execute(query, tuple(params))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "session_id": row["session_id"],
                "user_id": row["user_id"],
                "plan_hash": row["plan_hash"],
                "plan_version": row["plan_version"],
                "status": row["status"],
                "task_id": row["task_id"],
                "created_at": str(row["created_at"]),
                "expires_at": str(row["expires_at"]),
                "revoked_at": str(row["revoked_at"]) if row["revoked_at"] else None,
                "metadata": json.loads(row["metadata"] or "{}") if isinstance(row["metadata"], str) else row["metadata"],
            }
        return None

    def is_plan_approved(
        self,
        session_id: str,
        plan_hash: str,
        task_id: Optional[str] = None,
    ) -> bool:
        """
        Verify whether an active, unexpired, non-revoked approval exists
        matching the exact session_id and canonical plan_hash.
        """
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                if task_id:
                    cur.execute(
                        """
                        SELECT 1 FROM plan_approvals
                        WHERE session_id = %s
                          AND plan_hash = %s
                          AND status = 'approved'
                          AND revoked_at IS NULL
                          AND expires_at > NOW()
                          AND (task_id = %s OR task_id IS NULL)
                        LIMIT 1;
                        """,
                        (session_id, plan_hash, task_id),
                    )
                else:
                    cur.execute(
                        """
                        SELECT 1 FROM plan_approvals
                        WHERE session_id = %s
                          AND plan_hash = %s
                          AND status = 'approved'
                          AND revoked_at IS NULL
                          AND expires_at > NOW()
                        LIMIT 1;
                        """,
                        (session_id, plan_hash),
                    )
                return cur.fetchone() is not None
        elif self._sqlite_conn:
            cur = self._sqlite_conn.cursor()
            if task_id:
                cur.execute(
                    """
                    SELECT 1 FROM plan_approvals
                    WHERE session_id = ?
                      AND plan_hash = ?
                      AND status = 'approved'
                      AND revoked_at IS NULL
                      AND expires_at > ?
                      AND (task_id = ? OR task_id IS NULL)
                    LIMIT 1;
                    """,
                    (session_id, plan_hash, now_str, task_id),
                )
            else:
                cur.execute(
                    """
                    SELECT 1 FROM plan_approvals
                    WHERE session_id = ?
                      AND plan_hash = ?
                      AND status = 'approved'
                      AND revoked_at IS NULL
                      AND expires_at > ?
                    LIMIT 1;
                    """,
                    (session_id, plan_hash, now_str),
                )
            return cur.fetchone() is not None
        return False

    def invalidate_plan_approvals(self, session_id: str, reason: str = "plan_invalidated") -> int:
        """
        Invalidate all active approvals for a session when the plan DAG is modified.
        """
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()
        reason_meta = json.dumps({"revocation_reason": reason})

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE plan_approvals
                    SET status = 'revoked', revoked_at = NOW()
                    WHERE session_id = %s AND revoked_at IS NULL;
                    """,
                    (session_id,),
                )
                return cur.rowcount
        elif self._sqlite_conn:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                """
                UPDATE plan_approvals
                SET status = 'revoked', revoked_at = ?
                WHERE session_id = ? AND revoked_at IS NULL;
                """,
                (now_str, session_id),
            )
            self._sqlite_conn.commit()
            return cur.rowcount
        return 0

    # ── Phase 3: Execution Evidence Methods ─────────────────────────────────

    def record_evidence(
        self,
        id: str,
        user_id: str,
        session_id: str,
        plan_id: str,
        plan_hash: str,
        task_id: str,
        task_name: str,
        evidence_type: str,
        artifact_hash: str,
        verification_status: str = "inconclusive",
        verification_reason: Optional[str] = None,
        confidence_score: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
        content_payload: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Persists a cryptographically hashed evidence record with tenant isolation.
        """
        now_dt = datetime.now(timezone.utc)
        meta_json = json.dumps(metadata or {})

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO execution_evidence (
                        id, user_id, session_id, plan_id, plan_hash,
                        task_id, task_name, evidence_type, artifact_hash,
                        captured_at, verification_status, verification_reason,
                        confidence_score, metadata, content_payload
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    ) RETURNING *;
                    """,
                    (
                        id, user_id, session_id, plan_id, plan_hash,
                        task_id, task_name, evidence_type, artifact_hash,
                        now_dt, verification_status, verification_reason,
                        confidence_score, meta_json, content_payload,
                    ),
                )
                row = cur.fetchone()
                cols = [desc[0] for desc in cur.description]
                return dict(zip(cols, row))
        elif self._sqlite_conn:
            cur = self._sqlite_conn.cursor()
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO users (id, full_name, email) VALUES (?, ?, ?)",
                    (user_id, f"User {user_id}", f"{user_id}@ardo.ai"),
                )
            except Exception:
                pass
            cur.execute(
                """
                INSERT INTO execution_evidence (
                    id, user_id, session_id, plan_id, plan_hash,
                    task_id, task_name, evidence_type, artifact_hash,
                    captured_at, verification_status, verification_reason,
                    confidence_score, metadata, content_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    id, user_id, session_id, plan_id, plan_hash,
                    task_id, task_name, evidence_type, artifact_hash,
                    now_dt.isoformat(), verification_status, verification_reason,
                    confidence_score, meta_json, content_payload,
                ),
            )
            self._sqlite_conn.commit()
            return {
                "id": id,
                "user_id": user_id,
                "session_id": session_id,
                "plan_id": plan_id,
                "plan_hash": plan_hash,
                "task_id": task_id,
                "task_name": task_name,
                "evidence_type": evidence_type,
                "artifact_hash": artifact_hash,
                "captured_at": now_dt.isoformat(),
                "verification_status": verification_status,
                "verification_reason": verification_reason,
                "confidence_score": confidence_score,
                "metadata": metadata or {},
                "content_payload": content_payload,
            }
        return {}

    def get_session_evidence(
        self, session_id: str, user_id: str
    ) -> List[Dict[str, Any]]:
        """
        Retrieves evidence items for a session, strictly enforcing tenant isolation.
        """
        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT * FROM execution_evidence
                    WHERE session_id = %s AND user_id = %s
                    ORDER BY captured_at ASC;
                    """,
                    (session_id, user_id),
                )
                cols = [desc[0] for desc in cur.description]
                rows = cur.fetchall()
                results = []
                for row in rows:
                    d = dict(zip(cols, row))
                    if isinstance(d.get("metadata"), str):
                        try:
                            d["metadata"] = json.loads(d["metadata"])
                        except Exception:
                            pass
                    results.append(d)
                return results
        elif self._sqlite_conn:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                """
                SELECT * FROM execution_evidence
                WHERE session_id = ? AND user_id = ?
                ORDER BY captured_at ASC;
                """,
                (session_id, user_id),
            )
            cols = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            results = []
            for row in rows:
                d = dict(zip(cols, row))
                if isinstance(d.get("metadata"), str):
                    try:
                        d["metadata"] = json.loads(d["metadata"])
                    except Exception:
                        pass
                results.append(d)
            return results
        return []

    def get_evidence_by_id(
        self, evidence_id: str, user_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieves a single evidence record with strict user isolation.
        """
        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT * FROM execution_evidence
                    WHERE id = %s AND user_id = %s;
                    """,
                    (evidence_id, user_id),
                )
                row = cur.fetchone()
                if row:
                    cols = [desc[0] for desc in cur.description]
                    d = dict(zip(cols, row))
                    if isinstance(d.get("metadata"), str):
                        try:
                            d["metadata"] = json.loads(d["metadata"])
                        except Exception:
                            pass
                    return d
        elif self._sqlite_conn:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                """
                SELECT * FROM execution_evidence
                WHERE id = ? AND user_id = ?;
                """,
                (evidence_id, user_id),
            )
            row = cur.fetchone()
            if row:
                cols = [desc[0] for desc in cur.description]
                d = dict(zip(cols, row))
                if isinstance(d.get("metadata"), str):
                    try:
                        d["metadata"] = json.loads(d["metadata"])
                    except Exception:
                        pass
                return d
        return None

    # ── Phase 3: Playbooks Methods ──────────────────────────────────────────

    def record_playbook(
        self,
        id: str,
        user_id: str,
        goal_pattern: str,
        workflow_category: str = "general",
        preconditions: Optional[List[str]] = None,
        execution_steps: Optional[List[Dict[str, Any]]] = None,
        required_tools: Optional[List[str]] = None,
        expected_outcomes: Optional[List[str]] = None,
        supporting_evidence_ids: Optional[List[str]] = None,
        provenance_session_id: Optional[str] = None,
        provenance_plan_hash: Optional[str] = None,
        status: str = "candidate",
        confidence_score: float = 0.5,
        version: int = 1,
        metadata: Optional[Dict[str, Any]] = None,
        category: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Persists a reusable playbook with tenant isolation and provenance hash.
        """
        workflow_category = category or workflow_category or "general"
        preconditions = preconditions or []
        execution_steps = execution_steps or []
        required_tools = required_tools or []
        expected_outcomes = expected_outcomes or []
        supporting_evidence_ids = supporting_evidence_ids or []
        provenance_session_id = provenance_session_id or ""
        provenance_plan_hash = provenance_plan_hash or ""

        now_dt = datetime.now(timezone.utc)
        meta_json = json.dumps(metadata or {})
        prec_json = json.dumps(preconditions)
        steps_json = json.dumps(execution_steps)
        tools_json = json.dumps(required_tools)
        outcomes_json = json.dumps(expected_outcomes)
        ev_json = json.dumps(supporting_evidence_ids)

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO playbooks (
                        id, version, user_id, workflow_category, goal_pattern,
                        preconditions, execution_steps, required_tools, expected_outcomes,
                        supporting_evidence_ids, provenance_session_id, provenance_plan_hash,
                        status, confidence_score, success_count, failure_count,
                        created_at, last_used_at, metadata
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, 0, %s, %s, %s
                    ) RETURNING *;
                    """,
                    (
                        id, version, user_id, workflow_category, goal_pattern,
                        prec_json, steps_json, tools_json, outcomes_json,
                        ev_json, provenance_session_id, provenance_plan_hash,
                        status, confidence_score, now_dt, now_dt, meta_json,
                    ),
                )
                row = cur.fetchone()
                cols = [desc[0] for desc in cur.description]
                return dict(zip(cols, row))
        elif self._sqlite_conn:
            cur = self._sqlite_conn.cursor()
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO users (id, full_name, email) VALUES (?, ?, ?)",
                    (user_id, f"User {user_id}", f"{user_id}@ardo.ai"),
                )
            except Exception:
                pass
            cur.execute(
                """
                INSERT INTO playbooks (
                    id, version, user_id, workflow_category, goal_pattern,
                    preconditions, execution_steps, required_tools, expected_outcomes,
                    supporting_evidence_ids, provenance_session_id, provenance_plan_hash,
                    status, confidence_score, success_count, failure_count,
                    created_at, last_used_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?);
                """,
                (
                    id, version, user_id, workflow_category, goal_pattern,
                    prec_json, steps_json, tools_json, outcomes_json,
                    ev_json, provenance_session_id, provenance_plan_hash,
                    status, confidence_score, now_dt.isoformat(), now_dt.isoformat(), meta_json,
                ),
            )
            self._sqlite_conn.commit()
            return {
                "id": id,
                "version": version,
                "user_id": user_id,
                "workflow_category": workflow_category,
                "goal_pattern": goal_pattern,
                "preconditions": preconditions,
                "execution_steps": execution_steps,
                "required_tools": required_tools,
                "expected_outcomes": expected_outcomes,
                "supporting_evidence_ids": supporting_evidence_ids,
                "provenance_session_id": provenance_session_id,
                "provenance_plan_hash": provenance_plan_hash,
                "status": status,
                "confidence_score": confidence_score,
                "success_count": 1,
                "failure_count": 0,
                "created_at": now_dt.isoformat(),
                "last_used_at": now_dt.isoformat(),
                "metadata": metadata or {},
            }
        return {}

    def get_playbooks_for_user(
        self,
        user_id: str,
        category: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Retrieves playbooks for a user with optional category and status filtering.
        """
        query = "SELECT * FROM playbooks WHERE user_id = ?"
        params: List[Any] = [user_id]
        if category:
            query += " AND workflow_category = ?"
            params.append(category)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY confidence_score DESC, last_used_at DESC;"

        if self._is_pg and self._pg_conn:
            pg_query = query.replace("?", "%s")
            with self._pg_conn.cursor() as cur:
                cur.execute(pg_query, tuple(params))
                cols = [desc[0] for desc in cur.description]
                rows = cur.fetchall()
                results = []
                for row in rows:
                    d = dict(zip(cols, row))
                    for json_field in ("preconditions", "execution_steps", "required_tools", "expected_outcomes", "supporting_evidence_ids", "metadata"):
                        if isinstance(d.get(json_field), str):
                            try:
                                d[json_field] = json.loads(d[json_field])
                            except Exception:
                                pass
                    results.append(d)
                return results
        elif self._sqlite_conn:
            cur = self._sqlite_conn.cursor()
            cur.execute(query, tuple(params))
            cols = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            results = []
            for row in rows:
                d = dict(zip(cols, row))
                for json_field in ("preconditions", "execution_steps", "required_tools", "expected_outcomes", "supporting_evidence_ids", "metadata"):
                    if isinstance(d.get(json_field), str):
                        try:
                            d[json_field] = json.loads(d[json_field])
                        except Exception:
                            pass
                results.append(d)
            return results
        return []

    def get_playbook_by_id(
        self, playbook_id: str, user_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieves a playbook by ID, optionally enforcing tenant ownership.
        """
        query = "SELECT * FROM playbooks WHERE id = ?"
        params: List[Any] = [playbook_id]
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)

        if self._is_pg and self._pg_conn:
            pg_query = query.replace("?", "%s")
            with self._pg_conn.cursor() as cur:
                cur.execute(pg_query, tuple(params))
                row = cur.fetchone()
                if row:
                    cols = [desc[0] for desc in cur.description]
                    d = dict(zip(cols, row))
                    for json_field in ("preconditions", "execution_steps", "required_tools", "expected_outcomes", "supporting_evidence_ids", "metadata"):
                        if isinstance(d.get(json_field), str):
                            try:
                                d[json_field] = json.loads(d[json_field])
                            except Exception:
                                pass
                    return d
        elif self._sqlite_conn:
            cur = self._sqlite_conn.cursor()
            cur.execute(query, tuple(params))
            row = cur.fetchone()
            if row:
                cols = [desc[0] for desc in cur.description]
                d = dict(zip(cols, row))
                for json_field in ("preconditions", "execution_steps", "required_tools", "expected_outcomes", "supporting_evidence_ids", "metadata"):
                    if isinstance(d.get(json_field), str):
                        try:
                            d[json_field] = json.loads(d[json_field])
                        except Exception:
                            pass
                return d
        return None

    def update_playbook_status(
        self,
        playbook_id: str,
        status: str,
        user_id: Optional[str] = None,
        version_increment: bool = False,
    ) -> bool:
        """
        Updates playbook status (candidate -> promoted, deprecated, revoked) with version bump.
        """
        now_str = datetime.now(timezone.utc).isoformat()
        now_dt = datetime.now(timezone.utc)

        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                if version_increment:
                    sql = """
                    UPDATE playbooks
                    SET status = %s, version = version + 1, last_used_at = NOW()
                    WHERE id = %s
                    """
                else:
                    sql = """
                    UPDATE playbooks
                    SET status = %s, last_used_at = NOW()
                    WHERE id = %s
                    """
                params = [status, playbook_id]
                if user_id:
                    sql += " AND user_id = %s"
                    params.append(user_id)
                cur.execute(sql, tuple(params))
                return cur.rowcount > 0
        elif self._sqlite_conn:
            cur = self._sqlite_conn.cursor()
            if version_increment:
                sql = """
                UPDATE playbooks
                SET status = ?, version = version + 1, last_used_at = ?
                WHERE id = ?
                """
                params = [status, now_str, playbook_id]
            else:
                sql = """
                UPDATE playbooks
                SET status = ?, last_used_at = ?
                WHERE id = ?
                """
                params = [status, now_str, playbook_id]
            if user_id:
                sql += " AND user_id = ?"
                params.append(user_id)
            cur.execute(sql, tuple(params))
            self._sqlite_conn.commit()
            return cur.rowcount > 0
        return False

    def retry_failed_outbox_events(self, max_age_hours: int = 24) -> int:
        """
        Recovers outbox events previously marked 'failed' by resetting status to 'pending'
        and clearing attempts. Enables automatic recovery after database / network restores.
        """
        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE outbox_events
                    SET status = 'pending', attempts = 0
                    WHERE status = 'failed'
                      AND created_at >= NOW() - INTERVAL '%s hours';
                    """,
                    (max_age_hours,),
                )
                return cur.rowcount
        elif self._sqlite_conn:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                """
                UPDATE outbox_events
                SET status = 'pending', attempts = 0
                WHERE status = 'failed';
                """
            )
            self._sqlite_conn.commit()
            return cur.rowcount
        return 0

    def record_playbook_outcome(self, playbook_id: str, success: bool) -> None:
        """
        Updates success/failure counts and adjusts confidence score dynamically.
        """
        if self._is_pg and self._pg_conn:
            with self._pg_conn.cursor() as cur:
                if success:
                    cur.execute(
                        """
                        UPDATE playbooks
                        SET success_count = success_count + 1,
                            confidence_score = LEAST(0.99, confidence_score + 0.05),
                            last_used_at = NOW()
                        WHERE id = %s;
                        """,
                        (playbook_id,),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE playbooks
                        SET failure_count = failure_count + 1,
                            confidence_score = GREATEST(0.1, confidence_score - 0.15),
                            last_used_at = NOW()
                        WHERE id = %s;
                        """,
                        (playbook_id,),
                    )
        elif self._sqlite_conn:
            cur = self._sqlite_conn.cursor()
            now_str = datetime.now(timezone.utc).isoformat()
            if success:
                cur.execute(
                    """
                    UPDATE playbooks
                    SET success_count = success_count + 1,
                        confidence_score = MIN(0.99, confidence_score + 0.05),
                        last_used_at = ?
                    WHERE id = ?;
                    """,
                    (now_str, playbook_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE playbooks
                    SET failure_count = failure_count + 1,
                        confidence_score = MAX(0.1, confidence_score - 0.15),
                        last_used_at = ?
                    WHERE id = ?;
                    """,
                    (now_str, playbook_id),
                )
            self._sqlite_conn.commit()


