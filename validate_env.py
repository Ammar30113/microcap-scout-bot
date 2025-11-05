import importlib
import sys

REQUIRED_PACKAGES = [
    "fastapi",
    "uvicorn",
    "requests",
    "pandas",
    "yfinance",
    "finvizlite",
    "stockstats",
    "tenacity",
    "pydantic",
    "aiohttp",
    "httpx",
    "python_dotenv",
]

missing = []
for pkg in REQUIRED_PACKAGES:
    try:
        importlib.import_module(pkg.replace("-", "_"))
    except ImportError:
        missing.append(pkg)

if missing:
    print(f"❌ Missing dependencies: {', '.join(missing)}")
    sys.exit(1)
else:
    print("✅ All dependencies successfully imported.")
