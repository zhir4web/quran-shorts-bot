"""Vercel adapter for the existing Quran Shorts dashboard handler."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Vercel executes this file from /api, while the application modules and
# dashboard assets live at the repository root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard_server import DashboardHandler  # noqa: E402


_API_ROUTES = {"overview", "config", "workflow", "submit-clip"}


def normalize_vercel_api_path(path):
    """Restore the endpoint encoded in the query by vercel.json rewrites."""
    parsed = urlparse(path)
    routes = parse_qs(parsed.query).get("route", [])
    if parsed.path in ("", "/", "/api", "/api/") and len(routes) == 1:
        route = routes[0]
        if route in _API_ROUTES:
            return f"/api/{route}"
    return path


class handler(DashboardHandler):
    """Expose the existing handler through Vercel's Python runtime."""

    def __init__(self, request, client_address, server):
        server.github_token = os.environ.get("GITHUB_TOKEN", "")
        server.repository = os.environ.get("GITHUB_REPOSITORY", "zhir4web/quran-shorts-bot")
        server.branch = os.environ.get("GITHUB_BRANCH", "main")
        server.dashboard_key = os.environ.get("DASHBOARD_KEY", "")
        super().__init__(request, client_address, server)

    def do_GET(self):  # noqa: N802 - stdlib handler API
        self.path = normalize_vercel_api_path(self.path)
        super().do_GET()

    def do_POST(self):  # noqa: N802 - stdlib handler API
        self.path = normalize_vercel_api_path(self.path)
        super().do_POST()
