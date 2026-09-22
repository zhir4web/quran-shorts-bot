"""Vercel adapter for the existing Quran Shorts dashboard handler."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Vercel executes this file from /api, while the application modules and
# dashboard assets live at the repository root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard_server import DashboardHandler  # noqa: E402


class handler(DashboardHandler):
    """Expose the existing handler through Vercel's Python runtime."""

    def __init__(self, request, client_address, server):
        server.github_token = os.environ.get('GITHUB_TOKEN', '')
        server.repository = os.environ.get('GITHUB_REPOSITORY', 'zhir4web/quran-shorts-bot')
        server.branch = os.environ.get('GITHUB_BRANCH', 'main')
        server.dashboard_key = os.environ.get('DASHBOARD_KEY', '')
        super().__init__(request, client_address, server)
