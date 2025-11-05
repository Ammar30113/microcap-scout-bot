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

MODULE_ALIASES = {
    "python_dotenv": "dotenv",
}

missing = []
for pkg in REQUIRED_PACKAGES:
    module_name = pkg.replace("-", "_")
    module_name = MODULE_ALIASES.get(module_name, module_name)
    try:
        importlib.import_module(module_name)
    except ImportError:
        missing.append(pkg)

if missing:
    print(f"❌ Missing dependencies: {', '.join(missing)}")
    sys.exit(1)
else:
    print("✅ All dependencies successfully imported.")
