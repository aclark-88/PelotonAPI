import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure the project root is importable and force a throwaway SQLite DB per session.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"


@pytest.fixture()
def session():
    from app import db

    db.reset_state_for_tests()
    db.init_db()
    # Clean slate per test.
    from app.models import Base

    engine = db.get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with db.session_scope() as s:
        yield s
