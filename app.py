"""Vercel entrypoint for Feedline.

Vercel runs this file as the WSGI app. The React build is in dist/ and is
served by the Flask routes in server/feedline.py.
"""

import os
import secrets
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Vercel functions have a read-only filesystem except /tmp. Keep the local
# database path in /tmp unless a real database is configured with NEWS_DB.
os.environ.setdefault("NEWS_DB", str(Path(tempfile.gettempdir()) / "feedline.db"))

# Set NEWS_SECRET in Vercel Project Settings to keep sessions stable across
# cold starts. A generated fallback keeps the preview deploy usable.
if not os.environ.get("NEWS_SECRET"):
    os.environ["NEWS_SECRET"] = secrets.token_hex(32)

from server.feedline import app  # noqa: E402

__all__ = ["app"]
