from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.core.config import settings
from backend.api.router import router

app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    description="React Flow compatible goal-to-task AI decomposition engine backend.",
    docs_url=f"{settings.API_V1_STR}/docs"
)

# CORS Config using Synchronized Settings
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.ALLOWED_ORIGINS.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register endpoints mapped directly to '/api/v1' config
app.include_router(router, prefix=settings.API_V1_STR)
