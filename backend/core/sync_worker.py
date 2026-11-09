"""
sync_worker.py — Reliable Transactional Outbox Worker (PostgreSQL -> MongoDB)
=============================================================================
Manages reliable, asynchronous synchronization of authoritative PostgreSQL
user identity, OAuth account changes, and profile metadata into MongoDB Atlas
collection 'user_profiles'.

Key Architectural Rules:
- PostgreSQL remains the 100% authoritative identity and authentication store.
- MongoDB stores exclusively application-facing user profile and preferences.
- ZERO passwords, password hashes, session tokens, or OAuth secrets are ever
  sent to or stored in MongoDB.
- Transactional outbox pattern guarantees idempotency and zero duplicate profiles.
- Temporary MongoDB downtime never blocks authentication or crashes the engine.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.auth_db import AuthDatabase
from core.database import get_database

logger = logging.getLogger(__name__)


class ProfileSyncWorker:
    """
    Background worker that polls and processes pending outbox events,
    synchronizing them idempotently into MongoDB 'user_profiles'.
    """

    _instance: Optional["ProfileSyncWorker"] = None

    def __init__(self, poll_interval_seconds: float = 3.0):
        self.poll_interval = poll_interval_seconds
        self._is_running = False
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._last_recovery_time: float = 0.0

    @classmethod
    def get_instance(cls) -> "ProfileSyncWorker":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton and ensure background task is stopped cleanly."""
        if cls._instance is not None:
            cls._instance.stop_sync()
            cls._instance = None

    def start(self) -> None:
        """Start the background sync worker task."""
        if self._is_running and self._task and not self._task.done():
            return
        self._is_running = True
        self._stop_event = asyncio.Event()
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._run_loop())
            logger.info("[ProfileSyncWorker] Transactional Outbox sync worker started.")
        except RuntimeError:
            # Called outside a running event loop (e.g. during sync bootstrap)
            self._task = None
            self._is_running = False

    async def stop(self, timeout: float = 2.0) -> None:
        """Gracefully stop the sync worker task with bounded timeout."""
        self._is_running = False
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception as e:
                logger.debug(f"[ProfileSyncWorker] Exception during task stop: {e}")
        self._task = None
        logger.info("[ProfileSyncWorker] Transactional Outbox sync worker stopped.")

    def stop_sync(self, timeout: float = 2.0) -> None:
        """Synchronously request worker shutdown for test teardowns."""
        self._is_running = False
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                loop = self._task.get_loop()
                if not loop.is_running() and not loop.is_closed():
                    loop.run_until_complete(asyncio.wait_for(self._task, timeout=timeout))
            except Exception:
                pass
        self._task = None
        logger.info("[ProfileSyncWorker] Transactional Outbox sync worker stopped (sync).")

    async def _run_loop(self) -> None:
        """Continuous polling loop with bounded backoff and immediate cancellation on stop signal."""
        while self._is_running and (self._stop_event is None or not self._stop_event.is_set()):
            try:
                await self.process_pending_events()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[ProfileSyncWorker] Unexpected error in sync loop: {e}")

            if not self._is_running or (self._stop_event and self._stop_event.is_set()):
                break

            try:
                if self._stop_event is not None:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval)
                    break
                else:
                    await asyncio.sleep(self.poll_interval)
            except asyncio.TimeoutError:
                pass  # Normal poll interval elapsed
            except asyncio.CancelledError:
                break

    async def process_pending_events(self, batch_size: int = 20) -> int:
        """
        Fetch and process pending outbox events in batch.
        Returns the count of successfully processed events.
        """
        import time
        db_auth = AuthDatabase.get_instance()

        # Periodic recovery of failed events
        now = time.time()
        if now - self._last_recovery_time >= 60.0:
            self._last_recovery_time = now
            recovered = db_auth.retry_failed_outbox_events()
            if recovered > 0:
                logger.info(f"[ProfileSyncWorker] Recovered {recovered} failed outbox event(s) for retry.")
                try:
                    from core.metrics import metrics_collector
                    metrics_collector.record_outbox_retry(recovered)
                except Exception:
                    pass

        events = db_auth.fetch_pending_outbox_events(limit=batch_size)
        if not events:
            return 0

        mongo_db = get_database()
        if mongo_db is None:
            logger.debug("[ProfileSyncWorker] MongoDB is currently unavailable. Events remain queued in outbox.")
            return 0

        processed_count = 0
        for event in events:
            event_id = event["id"]
            user_id = event["aggregate_id"]
            try:
                await self._sync_user_to_mongodb(user_id, mongo_db)
                db_auth.mark_outbox_event_processed(event_id)
                processed_count += 1
            except Exception as e:
                logger.error(f"[ProfileSyncWorker] Failed to sync outbox event {event_id} (user={user_id}): {e}")
                db_auth.mark_outbox_event_failed(event_id, error_message=str(e))

        return processed_count

    async def sync_user_now(self, user_id: str) -> bool:
        """
        Synchronously dispatch an immediate sync for user_id to MongoDB.
        Useful for on-demand profile updates and test validation.
        """
        mongo_db = get_database()
        if mongo_db is None:
            return False
        try:
            await self._sync_user_to_mongodb(user_id, mongo_db)
            return True
        except Exception as e:
            logger.error(f"[ProfileSyncWorker] Immediate sync failed for user={user_id}: {e}")
            return False

    async def _sync_user_to_mongodb(self, user_id: str, mongo_db: Any) -> None:
        """
        Compile canonical profile from PostgreSQL and atomically upsert into MongoDB.
        Guarantees ZERO passwords, tokens, or credentials enter MongoDB.
        """
        db_auth = AuthDatabase.get_instance()
        user = db_auth.get_user_by_id(user_id)
        if not user:
            logger.warning(f"[ProfileSyncWorker] User {user_id} not found in PostgreSQL. Skipping MongoDB sync.")
            return

        # Fetch linked auth methods
        auth_methods = db_auth.count_user_auth_methods(user_id)
        auth_providers: List[str] = list(auth_methods.get("providers", []))
        if not auth_providers:
            auth_providers = ["oauth"] if not user.get("password_hash") else ["password"]

        # Check existing profile in MongoDB to preserve user-customized preferences
        existing_doc = await mongo_db.user_profiles.find_one({"_id": user_id})

        default_prefs = {"theme": "dark", "notifications_enabled": True}
        preferences = existing_doc.get("preferences", default_prefs) if existing_doc else default_prefs
        profile_picture = existing_doc.get("profile_picture") if existing_doc else None

        # Build clean document (NO credentials, NO secrets)
        now_iso = datetime.now(timezone.utc).isoformat()
        profile_doc: Dict[str, Any] = {
            "_id": user_id,
            "user_id": user_id,
            "email": user["email"],
            "full_name": user["full_name"],
            "profile_picture": profile_picture,
            "account_status": "active" if user.get("is_active", True) else "suspended",
            "auth_providers": auth_providers,
            "preferences": preferences,
            "created_at": user.get("created_at", now_iso),
            "updated_at": now_iso,
            "last_login_at": user.get("last_login_at"),
            "profile_sync_version": 1,
        }

        # Idempotent upsert
        await mongo_db.user_profiles.update_one(
            {"_id": user_id},
            {"$set": profile_doc},
            upsert=True,
        )
        logger.info(f"[ProfileSyncWorker] Successfully synchronized user {user_id} ({user['email']}) to MongoDB.")

