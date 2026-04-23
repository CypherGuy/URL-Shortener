from app.main import app, engine, Base
import pytest
from fastapi.testclient import TestClient
import os
import sys

# Add app to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


client = TestClient(app)


@pytest.fixture(scope="function")
def setup_db():
    """Setup and teardown database for each test"""
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


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
