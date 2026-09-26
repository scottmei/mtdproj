"""Serve the web app on http://localhost:8080.

Usage: python scripts/run_web.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

from busapp import config  # noqa: E402
from busapp.web import create_app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(create_app(), host=config.WEB_HOST, port=config.WEB_PORT)
