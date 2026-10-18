import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "py_modules"))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
# Overlay preview images are regenerated into docs/previews only on request
# (WFH_WRITE_PREVIEWS=1), so a plain test run leaves tracked files alone.
PREVIEWS_DIR = (
    os.path.join(ROOT, "docs", "previews")
    if os.environ.get("WFH_WRITE_PREVIEWS") == "1"
    else os.path.join(os.environ.get("TMPDIR", "/tmp"), "wfh-previews")
)
os.makedirs(PREVIEWS_DIR, exist_ok=True)


@pytest.fixture(scope="session")
def app(tmp_path_factory):
    """A headless worker App with all modules loaded (no network calls at setup)."""
    from wfh import app as app_mod

    base = tmp_path_factory.mktemp("wfh")
    a = app_mod.create(ROOT, str(base / "settings"), str(base / "runtime"), str(base / "log"), headless=True)
    os.makedirs(base / "settings", exist_ok=True)
    a.services["failed_modules"] = app_mod.load_modules(a)
    return a


@pytest.fixture
def fixtures_dir():
    return FIXTURES
