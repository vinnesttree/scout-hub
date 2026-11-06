import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point the app at throwaway dirs before anything imports app.config.
_tmp = Path(tempfile.mkdtemp(prefix="scouthub_test_"))
os.environ.setdefault("SCOUTHUB_DATA_DIR", str(_tmp / "data"))
os.environ.setdefault("SCOUTHUB_ARCHIVE_ROOT", str(_tmp / "media"))
(_tmp / "data").mkdir(parents=True, exist_ok=True)
(_tmp / "media").mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="session")
def archive_root() -> Path:
    return Path(os.environ["SCOUTHUB_ARCHIVE_ROOT"])


TEST_PASSWORD = "correct-horse-battery"


def sign_in(client, username: str = "testadmin", *, is_admin: bool = True) -> None:
    """Ensure an account exists and sign ``client`` in (the cookie sticks to it)."""
    from app.db import init_db, session_scope
    from app.services import auth as auth_svc

    init_db()
    with session_scope() as s:
        if auth_svc.get_by_username(s, username) is None:
            auth_svc.create_user(s, username, TEST_PASSWORD, is_admin=is_admin)
    r = client.post("/api/auth/login", json={"username": username, "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text
