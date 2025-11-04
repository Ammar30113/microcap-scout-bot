import logging

from fastapi import FastAPI

from combine_signals import router as combined_router
from microcap_scanner import ConfigurationError, gather_products
from social_scanner import router as social_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
LOGGER = logging.getLogger(__name__)

app = FastAPI(title="Microcap Scout v2", version="2.0.0")

app.include_router(social_router)
app.include_router(combined_router)


@app.get("/")
def healthcheck():
    """Simple readiness probe for Railway."""
    return {"status": "ok", "service": "Microcap Scout v2"}


@app.get("/products.json")
def products():
    """Return the latest combined microcap scan results."""
    try:
        results = gather_products(limit=10)
    except ConfigurationError as exc:
        LOGGER.error("Configuration error: %s", exc)
        return {"message": str(exc)}

    if not results:
        LOGGER.warning("No microcap data available to return.")
        return {"message": "Data unavailable"}

    return {"results": results}
