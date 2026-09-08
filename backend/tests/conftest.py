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
