import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Secrets, load_settings  # noqa: E402
from app.database.db import Database  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    return load_settings(overrides={"paths": {"database": str(tmp_path / "t.sqlite3"),
                                              "log_dir": str(tmp_path / "logs")},
                                    "app": {"environment": "test"},
                                    "execution": {"paper_leverage": 1},
                                    "news": {"finbert_enabled": False}})


@pytest.fixture
def no_secrets():
    return Secrets()


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "db.sqlite3")
    d.migrate()
    yield d
    d.close()
