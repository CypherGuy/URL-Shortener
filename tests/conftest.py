import os
import sqlite3
import tempfile
import atexit
import shutil
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["READ_REPLICA_URL"] = "sqlite:///:memory:"

_tmpdir = tempfile.mkdtemp()
_TEST_DB = os.path.join(_tmpdir, "test.db")
atexit.register(shutil.rmtree, _tmpdir, ignore_errors=True)

# Initialise WAL mode once on a single connection before concurrent tests open
# their own connections — avoids a race in pysqlite when threads all try to
# write the WAL header simultaneously on a fresh file.
with sqlite3.connect(_TEST_DB) as _init:
    _init.execute("PRAGMA journal_mode=WAL")


def _make_test_engine(url: str, **_kwargs):  # **_kwargs absorbs pool_size / max_overflow from make_engine callers
    def connect():
        conn = sqlite3.connect(_TEST_DB, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
    return create_engine("sqlite+pysqlite://", creator=connect, poolclass=NullPool)


_patcher = patch("app.db.make_engine", side_effect=_make_test_engine)
_patcher.start()
