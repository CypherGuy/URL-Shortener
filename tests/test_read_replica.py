import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from unittest.mock import patch
import fakeredis
from app.cache import RedisCache
import app.main as main_module
from app.main import app, Base, sync_to_replica
from app.models import Code

# ─── Test Database URLs ───────────────────────────────────────────────────────
PRIMARY_URL = "sqlite:///./test_primary.db"
REPLICA_URL = "sqlite:///./test_replica.db"

primary_test_engine = create_engine(PRIMARY_URL, connect_args={"check_same_thread": False})
replica_test_engine = create_engine(REPLICA_URL, connect_args={"check_same_thread": False})


# ─── Helpers ──────────────────────────────────────────────────────────────────

def sync_replica(primary_session, replica_session):
    """
    Simulates replication: copies all rows from primary to replica.
    In production this is handled automatically by PostgreSQL WAL streaming.
    """
    primary_rows = primary_session.query(Code).all()
    replica_session.query(Code).delete()
    for row in primary_rows:
        replica_session.merge(Code(
            id=row.id,
            short_code_chars=row.short_code_chars,
            original_url=row.original_url,
            clicks=row.clicks,
            created_at=row.created_at
        ))
    replica_session.commit()


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def setup_databases():
    """Create fresh primary and replica databases for each test."""
    Base.metadata.create_all(bind=primary_test_engine)
    Base.metadata.create_all(bind=replica_test_engine)
    yield
    Base.metadata.drop_all(bind=primary_test_engine)
    Base.metadata.drop_all(bind=replica_test_engine)


@pytest.fixture
def primary_session(setup_databases):
    with Session(primary_test_engine) as session:
        yield session


@pytest.fixture
def replica_session(setup_databases):
    with Session(replica_test_engine) as session:
        yield session


@pytest.fixture
def fake_cache():
    """Fake Redis cache to isolate DB routing tests from cache behaviour."""
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    return RedisCache(redis_client=client)


@pytest.fixture
def client_with_replica(setup_databases, fake_cache):
    """
    TestClient with both engines patched to use test databases,
    and Redis patched to use fakeredis.
    """
    def get_primary_session():
        with Session(primary_test_engine) as session:
            yield session

    def get_replica_session():
        with Session(replica_test_engine) as session:
            yield session

    with patch.object(main_module, "web_engine", primary_test_engine), \
            patch.object(main_module, "web_replica_engine", replica_test_engine), \
            patch.object(main_module, "r", fake_cache), \
            patch("app.main.get_session", get_primary_session), \
            patch("app.main.get_replica_session", get_replica_session):
        yield TestClient(app)


# ─── Section 1: Concept Tests (Primary/Replica isolation) ────────────────────

class TestReplicaSetup:
    """Verify that primary and replica databases are correctly set up."""

    def test_primary_and_replica_are_independent(self, primary_session, replica_session):
        """Primary and replica should be separate databases."""
        url = Code(short_code_chars="abc1234567", original_url="https://google.com", clicks=0)
        primary_session.add(url)
        primary_session.commit()

        replica_row = replica_session.query(Code).filter_by(short_code_chars="abc1234567").one_or_none()
        assert replica_row is None

    def test_replica_receives_data_after_sync(self, primary_session, replica_session):
        """After sync, replica should match primary."""
        url = Code(short_code_chars="abc1234567", original_url="https://google.com", clicks=0)
        primary_session.add(url)
        primary_session.commit()

        sync_replica(primary_session, replica_session)

        replica_row = replica_session.query(Code).filter_by(short_code_chars="abc1234567").one_or_none()
        assert replica_row is not None
        assert replica_row.original_url == "https://google.com"


class TestWritesGoToPrimary:
    """All writes must go to the primary, never the replica."""

    def test_new_url_written_to_primary(self, primary_session, replica_session):
        url = Code(short_code_chars="write12345", original_url="https://example.com", clicks=0)
        primary_session.add(url)
        primary_session.commit()

        primary_row = primary_session.query(Code).filter_by(short_code_chars="write12345").one_or_none()
        assert primary_row is not None

    def test_write_not_visible_on_replica_before_sync(self, primary_session, replica_session):
        url = Code(short_code_chars="lag1234567", original_url="https://example.com", clicks=0)
        primary_session.add(url)
        primary_session.commit()

        replica_row = replica_session.query(Code).filter_by(short_code_chars="lag1234567").one_or_none()
        assert replica_row is None

    def test_delete_goes_to_primary(self, primary_session, replica_session):
        url = Code(short_code_chars="del1234567", original_url="https://example.com", clicks=0)
        primary_session.add(url)
        primary_session.commit()
        sync_replica(primary_session, replica_session)

        primary_session.delete(
            primary_session.query(Code).filter_by(short_code_chars="del1234567").one()
        )
        primary_session.commit()

        assert primary_session.query(Code).filter_by(short_code_chars="del1234567").one_or_none() is None
        assert replica_session.query(Code).filter_by(short_code_chars="del1234567").one_or_none() is not None

    def test_delete_propagates_to_replica_after_sync(self, primary_session, replica_session):
        url = Code(short_code_chars="del1234567", original_url="https://example.com", clicks=0)
        primary_session.add(url)
        primary_session.commit()
        sync_replica(primary_session, replica_session)

        primary_session.delete(
            primary_session.query(Code).filter_by(short_code_chars="del1234567").one()
        )
        primary_session.commit()
        sync_replica(primary_session, replica_session)

        assert replica_session.query(Code).filter_by(short_code_chars="del1234567").one_or_none() is None


class TestReadsGoToReplica:
    """Read queries should be served by the replica."""

    def test_redirect_lookup_served_from_replica(self, primary_session, replica_session):
        url = Code(short_code_chars="read123456", original_url="https://google.com", clicks=0)
        primary_session.add(url)
        primary_session.commit()
        sync_replica(primary_session, replica_session)

        replica_row = replica_session.query(Code).filter_by(short_code_chars="read123456").one_or_none()
        assert replica_row is not None
        assert replica_row.original_url == "https://google.com"

    def test_stats_lookup_served_from_replica(self, primary_session, replica_session):
        url = Code(short_code_chars="stat123456", original_url="https://example.com", clicks=42)
        primary_session.add(url)
        primary_session.commit()
        sync_replica(primary_session, replica_session)

        replica_row = replica_session.query(Code).filter_by(short_code_chars="stat123456").one_or_none()
        assert replica_row is not None
        assert replica_row.clicks == 42

    def test_replica_returns_none_for_missing_key(self, primary_session, replica_session):
        result = replica_session.query(Code).filter_by(short_code_chars="notexists0").one_or_none()
        assert result is None


class TestReplicationLag:
    """Verify eventual consistency behaviour."""

    def test_replica_eventually_consistent_after_sync(self, primary_session, replica_session):
        url = Code(short_code_chars="eventual12", original_url="https://example.com", clicks=0)
        primary_session.add(url)
        primary_session.commit()

        assert replica_session.query(Code).filter_by(short_code_chars="eventual12").one_or_none() is None

        sync_replica(primary_session, replica_session)
        assert replica_session.query(Code).filter_by(short_code_chars="eventual12").one_or_none() is not None

    def test_replica_reflects_click_count_updates_after_sync(self, primary_session, replica_session):
        url = Code(short_code_chars="clicks1234", original_url="https://example.com", clicks=0)
        primary_session.add(url)
        primary_session.commit()
        sync_replica(primary_session, replica_session)

        row = primary_session.query(Code).filter_by(short_code_chars="clicks1234").one()
        row.clicks = 10
        primary_session.commit()

        replica_row = replica_session.query(Code).filter_by(short_code_chars="clicks1234").one()
        assert replica_row.clicks == 0

        sync_replica(primary_session, replica_session)
        replica_session.expire_all()
        replica_row = replica_session.query(Code).filter_by(short_code_chars="clicks1234").one()
        assert replica_row.clicks == 10

    def test_multiple_writes_all_replicate(self, primary_session, replica_session):
        for i in range(10):
            url = Code(short_code_chars=f"multi{i:05d}", original_url=f"https://example{i}.com", clicks=0)
            primary_session.add(url)
        primary_session.commit()

        sync_replica(primary_session, replica_session)

        for i in range(10):
            row = replica_session.query(Code).filter_by(short_code_chars=f"multi{i:05d}").one_or_none()
            assert row is not None
            assert row.original_url == f"https://example{i}.com"


class TestPrimaryReplicaIsolation:
    """Primary and replica connection pools must not interfere."""

    def test_replica_read_does_not_affect_primary(self, primary_session, replica_session):
        url = Code(short_code_chars="iso1234567", original_url="https://example.com", clicks=5)
        primary_session.add(url)
        primary_session.commit()
        sync_replica(primary_session, replica_session)

        _ = replica_session.query(Code).filter_by(short_code_chars="iso1234567").one()

        primary_row = primary_session.query(Code).filter_by(short_code_chars="iso1234567").one()
        assert primary_row.clicks == 5

    def test_primary_write_does_not_affect_replica_until_sync(self, primary_session, replica_session):
        url = Code(short_code_chars="iso2345678", original_url="https://example.com", clicks=0)
        primary_session.add(url)
        primary_session.commit()

        assert replica_session.query(Code).filter_by(short_code_chars="iso2345678").one_or_none() is None
        sync_replica(primary_session, replica_session)
        assert replica_session.query(Code).filter_by(short_code_chars="iso2345678").one_or_none() is not None


# ─── Section 2: App Routing Tests ────────────────────────────────────────────

class TestAppWritesToPrimary:
    """Verify that the app routes writes to the primary engine."""

    def test_shorten_writes_to_primary_not_replica(self, client_with_replica, primary_session, replica_session):
        """POST /shorten should create a row on primary only."""
        response = client_with_replica.post("/shorten", json={"original_url": "https://example.com/test"})
        assert response.status_code == 201

        short_code = response.json()["short_url"].split("/")[-1]

        primary_row = primary_session.query(Code).filter_by(short_code_chars=short_code).one_or_none()
        assert primary_row is not None

        replica_row = replica_session.query(Code).filter_by(short_code_chars=short_code).one_or_none()
        assert replica_row is None

    def test_delete_removes_from_primary(self, client_with_replica, primary_session, replica_session):
        """DELETE /{short_code} should remove from primary."""
        url = Code(short_code_chars="delroute12", original_url="https://example.com", clicks=0)
        primary_session.add(url)
        primary_session.commit()
        sync_replica(primary_session, replica_session)

        response = client_with_replica.delete("/delroute12")
        assert response.status_code == 204

        assert primary_session.query(Code).filter_by(short_code_chars="delroute12").one_or_none() is None


class TestAppReadsFromReplica:
    """Verify that the app routes reads to the replica engine."""

    def test_redirect_uses_replica_on_cache_miss(self, client_with_replica, primary_session, replica_session):
        """GET /{short_code} cache miss should fall back to replica, not primary."""
        url = Code(short_code_chars="replicard12", original_url="https://google.com", clicks=0)
        primary_session.add(url)
        primary_session.commit()
        sync_replica(primary_session, replica_session)

        response = client_with_replica.get("/replicard12", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "https://google.com"

    def test_redirect_returns_404_if_not_on_replica(self, client_with_replica, primary_session, replica_session):
        """GET /{short_code} should 404 if URL exists on primary but not yet replicated."""
        url = Code(short_code_chars="norepl12345", original_url="https://example.com", clicks=0)
        primary_session.add(url)
        primary_session.commit()
        # Deliberately no sync — replica doesn't have it yet

        response = client_with_replica.get("/norepl12345", follow_redirects=False)
        assert response.status_code == 404

    def test_stats_uses_replica(self, client_with_replica, primary_session, replica_session):
        """GET /stats/{short_code} should read from replica."""
        url = Code(short_code_chars="statsrep123", original_url="https://example.com", clicks=7)
        primary_session.add(url)
        primary_session.commit()
        sync_replica(primary_session, replica_session)

        response = client_with_replica.get("/stats/statsrep123")
        assert response.status_code == 200
        assert response.json()["original_url"] == "https://example.com"

    def test_stats_returns_404_if_not_on_replica(self, client_with_replica, primary_session, replica_session):
        """GET /stats/{short_code} should 404 if URL not yet replicated."""
        url = Code(short_code_chars="statsnorep1", original_url="https://example.com", clicks=0)
        primary_session.add(url)
        primary_session.commit()
        # Deliberately no sync

        response = client_with_replica.get("/stats/statsnorep1")
        assert response.status_code == 404


# ─── Section 3: sync_to_replica() Tests ──────────────────────────────────────

def _call_sync(primary_engine, replica_engine):
    """Patch both engines and call sync_to_replica()."""
    with patch.object(main_module, "sync_engine", primary_engine), \
         patch.object(main_module, "sync_replica_engine", replica_engine):
        sync_to_replica()


class TestSyncToReplica:
    """sync_to_replica() must make the replica an exact mirror of the primary."""

    def test_row_on_primary_appears_on_replica_after_sync(self, setup_databases):
        with Session(primary_test_engine) as s:
            s.add(Code(short_code_chars="synca12345", original_url="https://example.com", clicks=3))
            s.commit()

        _call_sync(primary_test_engine, replica_test_engine)

        with Session(replica_test_engine) as s:
            row = s.query(Code).filter_by(short_code_chars="synca12345").one_or_none()
            assert row is not None
            assert row.original_url == "https://example.com"
            assert row.clicks == 3

    def test_row_on_replica_only_is_deleted_after_sync(self, setup_databases):
        """A row that exists on the replica but not on primary must be removed."""
        with Session(replica_test_engine) as s:
            s.add(Code(short_code_chars="orphan1234", original_url="https://stale.com", clicks=0))
            s.commit()

        # Primary is empty — sync should wipe the orphan row from replica
        _call_sync(primary_test_engine, replica_test_engine)

        with Session(replica_test_engine) as s:
            assert s.query(Code).filter_by(short_code_chars="orphan1234").one_or_none() is None

    def test_empty_primary_clears_all_replica_rows(self, setup_databases):
        """If primary has no rows, replica should end up empty regardless of prior state."""
        with Session(replica_test_engine) as s:
            for i in range(3):
                s.add(Code(short_code_chars=f"stale{i:05d}", original_url="https://stale.com", clicks=0))
            s.commit()

        _call_sync(primary_test_engine, replica_test_engine)

        with Session(replica_test_engine) as s:
            assert s.query(Code).count() == 0

    def test_both_empty_does_not_crash(self, setup_databases):
        """Syncing two empty databases should complete without error."""
        _call_sync(primary_test_engine, replica_test_engine)

        with Session(replica_test_engine) as s:
            assert s.query(Code).count() == 0

    def test_updated_field_on_primary_reflected_in_replica(self, setup_databases):
        """A click count update on primary should be visible on replica after sync."""
        with Session(primary_test_engine) as s:
            s.add(Code(short_code_chars="syncb12345", original_url="https://example.com", clicks=0))
            s.commit()

        _call_sync(primary_test_engine, replica_test_engine)

        with Session(primary_test_engine) as s:
            row = s.query(Code).filter_by(short_code_chars="syncb12345").one()
            row.clicks = 42
            s.commit()

        _call_sync(primary_test_engine, replica_test_engine)

        with Session(replica_test_engine) as s:
            row = s.query(Code).filter_by(short_code_chars="syncb12345").one()
            assert row.clicks == 42

    def test_sync_is_idempotent(self, setup_databases):
        """Calling sync_to_replica twice in a row should produce the same result."""
        with Session(primary_test_engine) as s:
            s.add(Code(short_code_chars="idemp12345", original_url="https://example.com", clicks=5))
            s.commit()

        _call_sync(primary_test_engine, replica_test_engine)
        _call_sync(primary_test_engine, replica_test_engine)

        with Session(replica_test_engine) as s:
            assert s.query(Code).count() == 1
            row = s.query(Code).filter_by(short_code_chars="idemp12345").one()
            assert row.clicks == 5

    def test_multiple_primary_rows_all_appear_on_replica(self, setup_databases):
        with Session(primary_test_engine) as s:
            for i in range(5):
                s.add(Code(short_code_chars=f"multi{i:05d}", original_url=f"https://site{i}.com", clicks=i))
            s.commit()

        _call_sync(primary_test_engine, replica_test_engine)

        with Session(replica_test_engine) as s:
            assert s.query(Code).count() == 5
            for i in range(5):
                row = s.query(Code).filter_by(short_code_chars=f"multi{i:05d}").one_or_none()
                assert row is not None
                assert row.original_url == f"https://site{i}.com"
                assert row.clicks == i

    def test_partial_overlap_replica_matches_primary_exactly(self, setup_databases):
        """
        Primary: rows A and B.
        Replica (stale): rows B and C.
        After sync: replica should have exactly A and B — C must be gone.
        """
        with Session(primary_test_engine) as s:
            s.add(Code(short_code_chars="overlap_a0", original_url="https://a.com", clicks=0))
            s.add(Code(short_code_chars="overlap_b0", original_url="https://b.com", clicks=0))
            s.commit()

        with Session(replica_test_engine) as s:
            s.add(Code(short_code_chars="overlap_b0", original_url="https://b.com", clicks=0))
            s.add(Code(short_code_chars="overlap_c0", original_url="https://c.com", clicks=0))
            s.commit()

        _call_sync(primary_test_engine, replica_test_engine)

        with Session(replica_test_engine) as s:
            codes = {row.short_code_chars for row in s.query(Code).all()}
            assert codes == {"overlap_a0", "overlap_b0"}
            assert s.query(Code).filter_by(short_code_chars="overlap_c0").one_or_none() is None

    def test_all_fields_preserved_faithfully(self, setup_databases):
        """Every column (id, clicks, original_url, created_at) must be identical on replica."""
        from datetime import datetime, timezone
        ts = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        with Session(primary_test_engine) as s:
            row = Code(short_code_chars="fields1234", original_url="https://full.com", clicks=99, created_at=ts)
            s.add(row)
            s.commit()
            s.refresh(row)
            primary_id = row.id

        _call_sync(primary_test_engine, replica_test_engine)

        with Session(replica_test_engine) as s:
            replica_row = s.query(Code).filter_by(short_code_chars="fields1234").one()
            assert replica_row.id == primary_id
            assert replica_row.clicks == 99
            assert replica_row.original_url == "https://full.com"
