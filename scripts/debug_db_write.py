import asyncio
import logging
from motor.motor_asyncio import AsyncIOMotorClient
import os
import sys
from datetime import datetime

# Add backend to path to use config
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
fromcore.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("debug_write")

async def debug_write():
    # Mask URI for security
    raw_uri = settings.MONGODB_URI
    if "@" in raw_uri:
        parts = raw_uri.split("@")
        masked_uri = f"{parts[0].split('//')[0]}//****:****@{parts[1]}"
    else:
        masked_uri = "mongodb://localhost:27017"

    logger.info(f"ENVIRONMENT: {settings.ENVIRONMENT}")
    logger.info(f"Connecting to MongoDB at: {masked_uri}")
    logger.info(f"DATABASE_NAME (target): {settings.DATABASE_NAME}")

    try:
        client = AsyncIOMotorClient(settings.MONGODB_URI)
        db = client[settings.DATABASE_NAME]
        
        logger.info(f"ACTIVE DATABASE: {db.name}")
        
        # Perform Write
        test_doc = {
            "public_id": "standalone_debug_verification",
            "content": "Atlas writes verified",
            "timestamp": datetime.utcnow()
        }
        
        logger.info("Attempting to insert test document into 'goals'...")
        result = await db.goals.insert_one(test_doc)
        logger.info(f"INSERT SUCCESS. ID: {result.inserted_id}")
        
        # Read Back
        logger.info("Attempting to read back the document...")
        verified_doc = await db.goals.find_one({"_id": result.inserted_id})
        logger.info(f"READ BACK SUCCESS: {verified_doc['public_id']}")
        
        # Get Collections
        collections = await db.list_collection_names()
        logger.info(f"Current Collections in {db.name}: {collections}")
        
        client.close()
        logger.info("\n=== VERIFICATION COMPLETE: ALL PIPELINES OPERATIONAL ===\n")
        
    except Exception as e:
        logger.error(f"VERIFICATION FAILED: {e}")

if __name__ == "__main__":
    asyncio.run(debug_write())
