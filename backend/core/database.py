import logging
from motor.motor_asyncio import AsyncIOMotorClient
from core.config import settings
from datetime import datetime

logger = logging.getLogger(__name__)

class Database:
    client: AsyncIOMotorClient = None
    db = None

    @classmethod
    async def connect_db(cls):
        """Standard connection initialize for FastAPI app startup."""
        # Runtime Verification: Mask URI after protocol and username
        raw_uri = settings.MONGODB_URI
        if "@" in raw_uri:
            parts = raw_uri.split("@")
            masked_uri = f"{parts[0].split('//')[0]}//****:****@{parts[1]}"
        else:
            masked_uri = "mongodb://localhost:27017" # Default or fallback

        logger.info("FASTAPI STARTUP INIT")
        logger.info(f"Connecting to MongoDB at: {masked_uri}")
        logger.info(f"DATABASE_NAME in settings: {settings.DATABASE_NAME}")

        cls.client = AsyncIOMotorClient(settings.MONGODB_URI, serverSelectionTimeoutMS=10000)
        
        # Mandatory Step 2: Atlas Connection Ping Test & Initialization
        try:
            await cls.client.admin.command("ping")
            logger.info("ATLAS PING SUCCESS")

            cls.db = cls.client[settings.DATABASE_NAME]
            logger.info(f"DB CONNECTED: {cls.db.name}")
            
            # Explicitly bootstrap collections and indexes only if connected
            await cls.bootstrap_collections()
            await cls.create_indexes()
            await cls.seed_bootstrap_data()
            logger.info("BOOTSTRAP COMPLETE")
        except Exception as e:
            logger.error(f"DATABASE INITIALIZATION FAILED: {e}")
            logger.warning("Server will attempt to continue in DEGRADED mode (AI Persistence disabled).")
            # We don't raise here to allow the app to be reachable for health checks

    @classmethod
    async def close_db(cls):
        """Cleanly close connection."""
        if cls.client is not None:
            cls.client.close()
            logger.info("MongoDB connection closed.")

    @classmethod
    async def check_health(cls):
        """Probes live MongoDB connection health and measures ping latency."""
        import time
        if cls.client is None:
            return {"status": "unhealthy", "backend": "mongodb", "error": "Database client is not connected"}
        start = time.time()
        try:
            await cls.client.admin.command("ping")
            latency_ms = round((time.time() - start) * 1000, 2)
            return {"status": "healthy", "backend": "mongodb", "latency_ms": latency_ms}
        except Exception as e:
            return {"status": "unhealthy", "backend": "mongodb", "error": str(e)}

    @classmethod
    async def bootstrap_collections(cls):
        """Forcing collection creation on startup."""
        required_collections = [
            "goals", "plans", "tasks", "executions",
            "feedback", "safety_flags", "memory",
            "users", "sessions", "user_profiles",
            # ARDO Orchestration collections
            "orchestration_logs", "execution_metrics", "task_runtime_state",
        ]
        
        existing_collections = await cls.db.list_collection_names()
        for collection in required_collections:
            if collection not in existing_collections:
                logger.info(f"Bootstrapping collection: {collection}")
                await cls.db.create_collection(collection)

    @classmethod
    async def seed_bootstrap_data(cls):
        """Insert a seed document into goals if empty for Compass visibility."""
        goal_count = await cls.db.goals.count_documents({})
        if goal_count == 0:
            logger.info("Inserting system bootstrap verification document...")
            await cls.db.goals.insert_one({
                "public_id": "system_bootstrap",
                "goal": "system bootstrap verification",
                "status": "initialized",
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            })

    @classmethod
    async def create_indexes(cls):
        """
        Creates necessary indexes for performance and search as requested.
        """
        try:
            # Common indexes across core entities
            await cls.db.goals.create_index("public_id", unique=True)
            await cls.db.goals.create_index("created_at")

            await cls.db.plans.create_index("public_id", unique=True)
            await cls.db.plans.create_index("goal_id")
            await cls.db.plans.create_index("created_at")

            await cls.db.tasks.create_index("public_id", unique=True)
            await cls.db.tasks.create_index("plan_id")
            await cls.db.tasks.create_index("created_at")

            await cls.db.executions.create_index("public_id", unique=True)
            await cls.db.executions.create_index("task_id")
            await cls.db.executions.create_index("created_at")

            # ARDO Orchestration indexes
            await cls.db.orchestration_logs.create_index("session_id")
            await cls.db.orchestration_logs.create_index("timestamp")
            await cls.db.orchestration_logs.create_index("event_type")

            await cls.db.execution_metrics.create_index("session_id", unique=True)
            await cls.db.execution_metrics.create_index("started_at")

            await cls.db.task_runtime_state.create_index("session_id", unique=True)

            # ARDO User Profiles (synchronized from PostgreSQL authoritative store)
            await cls.db.user_profiles.create_index("user_id", unique=True)
            await cls.db.user_profiles.create_index("email")

            logger.info("Database indexes verified/created.")
        except Exception as e:
            logger.error(f"Error creating indexes: {e}")

def get_database():
    """Dependency for getting DB instance."""
    return Database.db
