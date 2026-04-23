"""
PMS Generator API — FastAPI application entry point.
"""
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.routes.pms_routes import router as pms_router
from app.services import data_service
from app.services import db_service

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Generate Piping Material Specifications using AI and reference data",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files and templates
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
templates = Jinja2Templates(directory=str(settings.templates_dir))

# API routes
app.include_router(pms_router)


@app.on_event("startup")
async def startup():
    """Load pipe class data and initialize DB connection pool on startup."""
    logger.info("Starting %s v%s", settings.app_name, settings.app_version)
    classes = data_service.get_available_classes()
    logger.info("%d pipe classes loaded from embedded data.", len(classes))
    # Initialize PostgreSQL cache (gracefully skipped if DATABASE_URL not set)
    await db_service.init_pool()
    if db_service.is_available():
        logger.info("PostgreSQL cache enabled")
    else:
        logger.info("PostgreSQL cache disabled — using AI-only mode")
    logger.info("Ready.")


@app.on_event("shutdown")
async def shutdown():
    """Close DB connection pool on shutdown."""
    await db_service.close_pool()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main UI."""
    return templates.TemplateResponse(request, "index.html")


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    """Serve the admin database browser — renders tables from
    `pms_cache` and `pms_agent_sessions`. Backed by the
    `/api/admin/db/*` endpoints for data."""
    return templates.TemplateResponse(request, "admin.html")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": settings.app_version,
        "api_key_set": bool(settings.anthropic_api_key),
        "api_key_length": len(settings.anthropic_api_key),
        "api_key_prefix": settings.anthropic_api_key[:12] + "..." if settings.anthropic_api_key else "EMPTY",
        "model": settings.anthropic_model,
    }
