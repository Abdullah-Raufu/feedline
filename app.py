"""Vercel entrypoint for Feedline.

Vercel runs this file as the WSGI app. The React build is in dist/ and is
served by the Flask routes in server/feedline.py.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Vercel functions have a read-only filesystem except /tmp. Keep the local
# database path in /tmp unless a real database is configured with NEWS_DB.
os.environ.setdefault("NEWS_DB", str(Path(tempfile.gettempdir()) / "feedline.db"))

# The Flask app reads NEWS_SECRET first, then server/deployment_secret.txt.
# scripts/prepare-vercel.mjs creates that file during the Vercel build so every
# serverless invocation shares one secret and sessions stay valid.
from server.feedline import app  # noqa: E402

__all__ = ["app"]
