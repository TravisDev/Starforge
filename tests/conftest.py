"""
Shared pytest fixtures.

The Starforge app reads STARFORGE_DATA_DIR at import time to decide where
board.db and secret.key live. We set it to a fresh temp dir BEFORE importing
the app so tests never touch your real database.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Critical: set the data dir BEFORE app is imported.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="starforge-test-")
os.environ["STARFORGE_DATA_DIR"] = _TEST_DATA_DIR

# Make the project root importable so `import app` works regardless of cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="session")
def test_data_dir() -> Path:
    return Path(_TEST_DATA_DIR)


@pytest.fixture(scope="session")
def client():
    """In-process TestClient for the FastAPI app."""
    import app
    from fastapi.testclient import TestClient
    return TestClient(app.app)


@pytest.fixture(scope="session")
def admin_client(client):
    """A TestClient that has run /api/setup so subsequent requests are authenticated."""
    resp = client.post(
        "/api/setup",
        json={
            "email": "admin@example.com",
            "display_name": "Test Admin",
            "password": "starforge-test-password",
        },
    )
    assert resp.status_code == 200, resp.text
    return client
