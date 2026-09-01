from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from core.config import settings
from api.router import router
from core.database import Database

app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    description="React Flow compatible goal-to-task AI decomposition engine backend.",
    docs_url=f"{settings.API_V1_STR}/docs"
)

# Database lifecycle handlers
@app.on_event("startup")
async def startup_db_client():
    await Database.connect_db()

@app.on_event("shutdown")
async def shutdown_db_client():
    await Database.close_db()

# CORS Config using Synchronized Settings
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.ALLOWED_ORIGINS.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register endpoints mapped directly to '/api/v1' config and root
app.include_router(router, prefix=settings.API_V1_STR)
app.include_router(router)
