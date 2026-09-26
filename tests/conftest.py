import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from busapp import db  # noqa: E402


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    yield c
    c.close()
