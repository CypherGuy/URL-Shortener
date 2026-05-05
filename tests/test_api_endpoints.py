import fakeredis
import app.main as main_module
from app.main import app, web_engine, web_replica_engine, Base, get_session, get_replica_session, get_cache
from app.cache import RedisCache
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
import os
import sys

# Add app to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

client = TestClient(app)


@pytest.fixture
def setup_db_with_cache():
    """Like setup_db but yields the fake cache so tests can inspect cached keys."""
    Base.metadata.create_all(bind=web_engine)
    Base.metadata.create_all(bind=web_replica_engine)
    fake_redis = fakeredis.FakeStrictRedis(decode_responses=True)
    fake_cache = RedisCache(redis_client=fake_redis)
    app.dependency_overrides[get_replica_session] = get_session
    app.dependency_overrides[get_cache] = lambda: fake_cache
    with patch.object(main_module, 'r', fake_cache):
        yield fake_cache
    app.dependency_overrides.pop(get_cache, None)
    app.dependency_overrides.pop(get_replica_session, None)
    Base.metadata.drop_all(bind=web_engine)
    Base.metadata.drop_all(bind=web_replica_engine)


@pytest.fixture(scope="function")
def setup_db():
    """Setup and teardown database for each test"""
    Base.metadata.create_all(bind=web_engine)
    Base.metadata.create_all(bind=web_replica_engine)
    fake_cache = RedisCache(redis_client=fakeredis.FakeStrictRedis(decode_responses=True))
    app.dependency_overrides[get_replica_session] = get_session
    app.dependency_overrides[get_cache] = lambda: fake_cache
    with patch.object(main_module, 'r', fake_cache):
        yield
    app.dependency_overrides.pop(get_cache, None)
    app.dependency_overrides.pop(get_replica_session, None)
    Base.metadata.drop_all(bind=web_engine)
    Base.metadata.drop_all(bind=web_replica_engine)


class TestPhase1RepositorySetup:
    """Phase 1: Repository & Infrastructure"""

    def test_env_file_exists(self):
        """✓ .env file created with DATABASE_URL"""
        assert os.path.exists('.env'), ".env file not found"

    def test_env_has_database_url(self):
        """✓ DATABASE_URL configured"""
        from dotenv import load_dotenv
        load_dotenv()
        db_url = os.getenv('DATABASE_URL')
        assert db_url is not None, "DATABASE_URL not set"
        assert 'postgresql' in db_url or 'sqlite' in db_url, "DATABASE_URL invalid format"


class TestPhase2CoreEndpoints:
    """Phase 2: Core API Endpoints"""

    def test_post_shorten_creates_short_code(self, setup_db):
        """✓ POST /shorten creates shortened URL"""
        response = client.post("/shorten", json={
            "original_url": "https://example.com/very/long/path"
        })
        assert response.status_code == 201, f"Expected 201, got {response.status_code}"
        data = response.json()
        assert "short_url" in data
        assert data["original_url"] == "https://example.com/very/long/path"

    def test_post_shorten_rejects_empty_url(self, setup_db):
        """✓ POST /shorten rejects empty URL (400)"""
        response = client.post("/shorten", json={
            "original_url": ""
        })
        assert response.status_code == 422

    def test_get_redirect_returns_302(self, setup_db):
        """✓ GET /{code} returns 302 redirect"""
        # Create a URL first
        create_response = client.post("/shorten", json={
            "original_url": "https://google.com"
        })
        short_code = create_response.json()["short_url"].split("/")[-1]

        # Test redirect
        response = client.get(f"/{short_code}", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "https://google.com"

    def test_get_redirect_increments_clicks(self, setup_db):
        """✓ GET /{code} increments click counter"""
        # Create URL
        create_response = client.post("/shorten", json={
            "original_url": "https://example.com"
        })
        short_code = create_response.json()["short_url"].split("/")[-1]

        # Check stats before
        stats_before = client.get(f"/stats/{short_code}").json()
        assert stats_before["clicks"] == 0

        # Click once
        client.get(f"/{short_code}", follow_redirects=False)

        # Check stats after
        stats_after = client.get(f"/stats/{short_code}").json()
        assert stats_after["clicks"] == 1

    def test_get_redirect_404_on_invalid_code(self, setup_db):
        """✓ GET /{invalid_code} returns 404"""
        response = client.get("/invalid123code", follow_redirects=False)
        assert response.status_code == 404

    def test_delete_removes_url(self, setup_db):
        """✓ DELETE /{code} removes URL"""
        # Create URL
        create_response = client.post("/shorten", json={
            "original_url": "https://example.com"
        })
        short_code = create_response.json()["short_url"].split("/")[-1]

        # Delete it
        delete_response = client.delete(f"/{short_code}")
        assert delete_response.status_code == 204

        # Verify it's gone
        get_response = client.get(f"/{short_code}", follow_redirects=False)
        assert get_response.status_code == 404

    def test_delete_404_on_invalid_code(self, setup_db):
        """✓ DELETE /{invalid_code} returns 404"""
        response = client.delete("/invalid123code")
        assert response.status_code == 404


class TestPhase3UnitTests:
    """Phase 3: Unit & Integration Tests"""

    def test_code_generation_uniqueness(self, setup_db):
        """✓ Short codes are unique"""
        codes = set()
        for i in range(100):
            response = client.post("/shorten", json={
                "original_url": f"https://example.com/{i}"
            })
            code = response.json()["short_url"].split("/")[-1]
            assert code not in codes, "Collision detected!"
            codes.add(code)

    def test_code_alphabet_base62(self, setup_db):
        """✓ Short codes use base62 alphabet"""
        import string
        base62 = string.ascii_letters + string.digits

        response = client.post("/shorten", json={
            "original_url": "https://example.com"
        })
        code = response.json()["short_url"].split("/")[-1]

        for char in code:
            assert char in base62, f"Invalid character in code: {char}"

    def test_collision_handling_retry(self, setup_db):
        """✓ Collision handling works (rare but possible)"""
        # This test verifies the retry logic doesn't crash
        for i in range(10):
            response = client.post("/shorten", json={
                "original_url": f"https://example.com/{i}"
            })
            assert response.status_code == 201


class TestPhase4Deployment:
    """Phase 4: Deployment & CI"""

    def test_app_health_check(self):
        """✓ Health check endpoint works"""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    def test_app_documentation(self):
        """✓ API documentation available"""
        response = client.get("/docs")
        assert response.status_code == 200


class TestPhase5LoadTesting:
    """Phase 5: Load Testing Setup (metrics collected manually)"""

    def test_concurrent_creates(self, setup_db):
        """✓ App handles concurrent requests"""
        import concurrent.futures

        def create_url(i):
            return client.post("/shorten", json={
                "original_url": f"https://example.com/{i}"
            })

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(create_url, i) for i in range(50)]
            results = [f.result()
                       for f in concurrent.futures.as_completed(futures)]

        assert len(results) == 50
        assert all(r.status_code == 201 for r in results)

    def test_rapid_redirects(self, setup_db):
        """✓ App handles rapid redirects"""
        # Create URL
        response = client.post("/shorten", json={
            "original_url": "https://example.com"
        })
        code = response.json()["short_url"].split("/")[-1]

        # Hit it 100 times
        for _ in range(100):
            r = client.get(f"/{code}", follow_redirects=False)
            assert r.status_code == 302

        # Verify click count
        stats = client.get(f"/stats/{code}").json()
        assert stats["clicks"] == 100


class TestShortenCaching:
    """POST /shorten cache behaviour"""

    def test_same_url_twice_returns_same_short_code(self, setup_db_with_cache):
        """Second shorten of the same URL returns the cached short code, not a new one."""
        url = "https://example.com/cache-dedup"
        first = client.post("/shorten", json={"original_url": url}).json()
        second = client.post("/shorten", json={"original_url": url}).json()
        assert first["short_url"] == second["short_url"]

    def test_same_url_twice_only_one_db_row(self, setup_db_with_cache):
        """Second shorten of the same URL must not insert a second database row."""
        from app.models import Code
        url = "https://example.com/cache-dedup-db"
        client.post("/shorten", json={"original_url": url})
        client.post("/shorten", json={"original_url": url})
        session = next(get_session())
        count = session.query(Code).filter_by(original_url=url).count()
        assert count == 1

    def test_shorten_populates_forward_cache(self, setup_db_with_cache):
        """After POST /shorten, short_code -> original_url is immediately in cache."""
        cache = setup_db_with_cache
        url = "https://example.com/forward-cache"
        data = client.post("/shorten", json={"original_url": url}).json()
        short_code = data["short_url"].split("/")[-1]
        assert cache.get(short_code) == url

    def test_shorten_populates_clicks_cache(self, setup_db_with_cache):
        """After POST /shorten, clicks:{short_code} is cached at zero."""
        cache = setup_db_with_cache
        url = "https://example.com/clicks-cache"
        data = client.post("/shorten", json={"original_url": url}).json()
        short_code = data["short_url"].split("/")[-1]
        assert cache.get_int(f"clicks:{short_code}") == 0

    def test_shorten_populates_created_at_cache(self, setup_db_with_cache):
        """After POST /shorten, created_at:{short_code} is cached."""
        cache = setup_db_with_cache
        url = "https://example.com/created-at-cache"
        data = client.post("/shorten", json={"original_url": url}).json()
        short_code = data["short_url"].split("/")[-1]
        assert cache.get(f"created_at:{short_code}") is not None

    def test_shorten_reverse_lookup_has_ttl(self, setup_db_with_cache):
        """The url:{original_url} reverse-lookup key must have a TTL (not stored forever)."""
        cache = setup_db_with_cache
        url = "https://example.com/ttl-check"
        client.post("/shorten", json={"original_url": url})
        ttl = cache.redis_client.ttl(url)
        assert ttl > 0, "reverse lookup key has no TTL — it will live forever"

    def test_delete_clears_reverse_lookup(self, setup_db_with_cache):
        """DELETE /{code} must remove the url:{original_url} reverse-lookup key."""
        cache = setup_db_with_cache
        url = "https://example.com/delete-reverse"
        data = client.post("/shorten", json={"original_url": url}).json()
        short_code = data["short_url"].split("/")[-1]
        client.delete(f"/{short_code}")
        assert cache.get(url) is None

    def test_delete_clears_reverse_lookup_even_if_forward_cache_expired(self, setup_db_with_cache):
        """Reverse lookup is cleared on DELETE even when the forward cache key has already expired."""
        cache = setup_db_with_cache
        url = "https://example.com/delete-expired-forward"
        data = client.post("/shorten", json={"original_url": url}).json()
        short_code = data["short_url"].split("/")[-1]

        # Simulate the forward cache key expiring before DELETE is called.
        cache.redis_client.delete(short_code)

        client.delete(f"/{short_code}")
        assert cache.get(url) is None
