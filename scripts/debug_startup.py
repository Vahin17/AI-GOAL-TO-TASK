import asyncio
import logging
import traceback
import os
import sys

# Add project root to path
sys.path.append(".")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("startup_debug")

async def test_startup():
    try:
        logger.info("Importing app and Database...")
        frommain import app
        fromcore.database import Database
        fromcore.config import settings
        
        logger.info(f"Loaded URI: {settings.MONGODB_URI[:30]}...")
        logger.info(f"Loaded GEMINI KEY: {'PRESENT' if settings.GEMINI_API_KEY else 'MISSING'}")
        
        logger.info("Calling Database.connect_db()...")
        await Database.connect_db()
        logger.info("Database.connect_db() finished successfully.")
        
        await Database.close_db()
        logger.info("Test complete.")
    except Exception as e:
        logger.error(f"STARTUP FAILED: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(test_startup())
