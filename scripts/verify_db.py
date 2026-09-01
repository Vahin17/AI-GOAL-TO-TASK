import asyncio
import logging
from motor.motor_asyncio import AsyncIOMotorClient
import os
import sys

# Add backend to path to use config if needed
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
fromcore.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_db")

async def verify():
    logger.info(f"Connecting to MongoDB: {settings.MONGODB_URI[:20]}...")
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.DATABASE_NAME]
    
    logger.info(f"Active Database: {settings.DATABASE_NAME}")
    
    # Check collections
    collections = await db.list_collection_names()
    logger.info(f"Existing Collections: {collections}")
    
    required_collections = [
        "goals", "plans", "tasks", "executions", 
        "feedback", "safety_flags", "memory", 
        "users", "sessions"
    ]
    
    missing = [c for c in required_collections if c not in collections]
    if missing:
        logger.error(f"Missing Collections: {missing}")
    else:
        logger.info("All 9 required collections are present.")

    # Check Counts
    for coll in required_collections:
        count = await db[coll].count_documents({})
        logger.info(f"Count for '{coll}': {count}")
    
    # Check Bootstrap Seed
    seed_doc = await db.goals.find_one({"public_id": "system_bootstrap"})
    if seed_doc:
        logger.info(f"System Seed Document Found: {seed_doc}")
    else:
        logger.warning("System Seed Document Not Found in 'goals'.")

    client.close()

if __name__ == "__main__":
    asyncio.run(verify())
