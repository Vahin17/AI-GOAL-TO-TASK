import requests
import asyncio
import logging
from motor.motor_asyncio import AsyncIOMotorClient
import os
import sys
import time

# Add backend to path to use config
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
fromcore.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api_verify")

def trigger_api():
    url = "http://127.0.0.1:8000/api/v1/plan"
    payload = {"goal": "Automated system verification goal"}
    logger.info(f"Triggering API at {url}...")
    try:
        response = requests.post(url, json=payload, timeout=30)
        logger.info(f"API Response Status: {response.status_code}")
        if response.status_code == 200:
            logger.info(f"API Response Data: {response.json().get('public_id')}")
            return True
    except Exception as e:
        logger.error(f"API call failed: {e}")
    return False

async def verify_db():
    logger.info(f"Connecting to MongoDB: {settings.MONGODB_URI[:20]}...")
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.DATABASE_NAME]
    
    logger.info(f"Active Database: {settings.DATABASE_NAME}")
    
    collections = await db.list_collection_names()
    logger.info(f"Existing Collections: {collections}")
    
    required_collections = [
        "goals", "plans", "tasks", "executions", 
        "feedback", "safety_flags", "memory", 
        "users", "sessions"
    ]
    
    results = {}
    for coll in required_collections:
        count = await db[coll].count_documents({})
        results[coll] = count
        logger.info(f"Count for '{coll}': {count}")
    
    client.close()
    return results

if __name__ == "__main__":
    if trigger_api():
        # wait a bit for persistence to settle if async
        time.sleep(2)
        asyncio.run(verify_db())
    else:
        logger.error("Skipping DB verification because API trigger failed.")
