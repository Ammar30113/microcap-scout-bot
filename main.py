import os

import uvicorn

from app import app
from social_scanner import router as social_router
from combine_signals import router as combined_router

app.include_router(social_router)
app.include_router(combined_router)

__all__ = ["app"]

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
