# Repository Guidelines

## Project Structure & Module Organization
Core modules sit in the repo root. `app.py` runs the insider-trade polling loop that orchestrates order submission. `main.py` exposes the FastAPI service (see `Procfile` for deployment entrypoint). `data_sources.py` wraps outbound data fetches (Finviz, yfinance, sentiment). `trade_engine.py` centralizes risk rules, position tracking, and P&L logging. Configuration scaffolding lives in `config.example.json` and `settings.example.py`; copy either to a real file and fill in secrets. Keep generated artifacts (logs, caches) out of version control.

## Build, Test, and Development Commands
Create an isolated environment and install tools with `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`. Run the trading worker via `python app.py`. Start the API for local inspection with `uvicorn main:app --reload`. The project relies on `pytest`; execute `pytest` (optionally `pytest -k <pattern>`) before opening a PR. Use `black .` and `flake8` to normalize style prior to commits.

## Coding Style & Naming Conventions
Use Python 3.11+ and four-space indentation. Favor snake_case for functions, variables, and module names; use PascalCase only for classes. Keep FastAPI route names and query params short and descriptive (`/scan`, `ticker`). Write docstrings for public functions that hit external services and inline comments only where the trading logic is non-obvious. Run `black` and `flake8` locally; CI expects both.

## Testing Guidelines
House tests under a top-level `tests/` package with files named `test_<feature>.py`. Mock Alpaca and network calls (e.g., `responses` or `unittest.mock`) so suites run offline. Cover new trade rules, cache lifetimes, and failure scenarios for data sources. Add regression tests whenever a bug fix touches order sizing or API parsing. Target at least smoke coverage over API endpoints (`client.get("/scan")`) before merging.

## Commit & Pull Request Guidelines
Follow the existing Conventional Commit pattern (`fix: ...`, `refactor: ...`, `chore: ...`). Squash noisy work-in-progress commits before pushing. Each PR should include: a concise summary of the change, linked issue or ticket, screenshots or curl output for API-affecting work, and a checklist of commands run (`pytest`, `black`, `flake8`). Highlight any new environment variables or migrations in the PR description to reduce deployment surprises.

## Security & Configuration Notes
Never commit credentials; rely on environment variables (`APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `STOCKDATA_API_KEY`, `SCAN_TICKERS`). Keep `trading.log` and P&L artifacts restricted to local machines. When sharing config snippets, reference the example files instead of pasting real keys. Validate external endpoints in `data_sources.py` whenever dependencies are upgraded.
