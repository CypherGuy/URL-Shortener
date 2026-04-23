import pytest
import time
import fakeredis
from app.cache import RedisCache


@pytest.fixture
def fake_redis():
    """Fixture that provides a fake Redis instance"""
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def cache(fake_redis):
    """Fixture that provides a cache instance with fake Redis"""
    return RedisCache(redis_client=fake_redis)


class TestRedisConnection:
    """Test Redis connection and basic operations"""

    def test_cache_initializes_with_fake_redis(self, cache):
        """Test that cache can be initialized with a fake Redis client"""
        assert cache is not None
        assert cache.redis_client is not None

    def test_cache_ping(self, cache):
        """Test that we can ping Redis"""
        result = cache.redis_client.ping()
        assert result is True


class TestBasicCacheOperations:
    """Test basic SET/GET operations"""

    def test_set_and_get_simple_value(self, cache):
        """Test setting and getting a simple string value"""
        cache.set("test_key", "test_value")
        result = cache.get("test_key")
        assert result == "test_value"

    def test_get_nonexistent_key_returns_none(self, cache):
        """Test that getting a non-existent key returns None"""
        result = cache.get("nonexistent_key")
        assert result is None

    def test_delete_key(self, cache):
        """Test deleting a key"""
        cache.set("test_key", "test_value")
        assert cache.get("test_key") == "test_value"

        cache.delete("test_key")
        assert cache.get("test_key") is None

    def test_key_exists(self, cache):
        """Test checking if a key exists"""
        cache.set("test_key", "test_value")
        assert cache.exists("test_key") is True
        assert cache.exists("nonexistent_key") is False


class TestTTLOperations:
    """Test Time-To-Live functionality"""

    def test_set_with_ttl(self, cache):
        """Test setting a value with TTL"""
        cache.set("test_key", "test_value", ttl=3600)
        result = cache.get("test_key")
        assert result == "test_value"

    def test_set_with_short_ttl_expires(self, cache):
        """Test that a key expires after TTL"""
        cache.set("test_key", "test_value", ttl=1)
        assert cache.get("test_key") == "test_value"

        time.sleep(2)

        # In fakeredis, expired keys are cleaned up on access
        result = cache.get("test_key")
        assert result is None

    def test_default_ttl_is_one_hour(self, cache):
        """Test that default TTL is 3600 seconds (1 hour)"""
        cache.set("test_key", "test_value")
        ttl = cache.redis_client.ttl("test_key")
        # TTL should be around 3600 seconds
        assert ttl > 3590 and ttl <= 3600


class TestURLCachingUseCase:
    """Test caching for URL shortener use case"""

    def test_cache_short_code_mapping(self, cache):
        """Test caching short_code -> original_url mapping"""
        short_code = "abc123"
        original_url = "https://google.com"

        cache.set(short_code, original_url, ttl=3600)

        cached_url = cache.get(short_code)
        assert cached_url == original_url

    def test_multiple_short_codes(self, cache):
        """Test caching multiple short code mappings"""
        mappings = {
            "abc123": "https://google.com",
            "def456": "https://github.com",
            "xyz789": "https://stackoverflow.com"
        }

        for short_code, url in mappings.items():
            cache.set(short_code, url, ttl=3600)

        for short_code, expected_url in mappings.items():
            assert cache.get(short_code) == expected_url

    def test_cache_update_overwrites_old_value(self, cache):
        """Test that setting a key again overwrites the old value"""
        short_code = "abc123"

        cache.set(short_code, "https://google.com", ttl=3600)
        assert cache.get(short_code) == "https://google.com"

        cache.set(short_code, "https://github.com", ttl=3600)
        assert cache.get(short_code) == "https://github.com"


class TestCacheMissHandling:
    """Test handling of cache misses"""

    def test_cache_miss_on_first_access(self, cache):
        """Test that first access to a key returns None (cache miss)"""
        result = cache.get("never_set_key")
        assert result is None

    def test_cache_miss_after_deletion(self, cache):
        """Test that accessing a deleted key returns None"""
        cache.set("test_key", "test_value")
        cache.delete("test_key")

        result = cache.get("test_key")
        assert result is None


class TestClickCounterScenario:
    """Test click counter use case"""

    def test_increment_click_counter_in_redis(self, cache):
        """Test incrementing click counter in Redis"""
        short_code = "abc123"

        # Simulate multiple redirects
        for i in range(5):
            cache.increment(f"clicks:{short_code}")

        clicks = cache.get(f"clicks:{short_code}")
        assert int(clicks) == 5

    def test_multiple_short_codes_click_counters(self, cache):
        """Test tracking clicks for multiple short codes"""
        short_codes = ["abc123", "def456", "xyz789"]

        # Simulate different number of clicks for each
        for i, short_code in enumerate(short_codes):
            for _ in range(i + 1):
                cache.increment(f"clicks:{short_code}")

        assert int(cache.get("clicks:abc123")) == 1
        assert int(cache.get("clicks:def456")) == 2
        assert int(cache.get("clicks:xyz789")) == 3


class TestCacheIntegrationWithFastAPI:
    """Test cache integration with FastAPI endpoints"""

    def test_cache_pattern_for_redirect(self, cache):
        """Test the cache-aside pattern for GET /{short_code}"""
        short_code = "abc123"
        original_url = "https://google.com"

        # First access - cache miss (simulating)
        cached = cache.get(short_code)
        assert cached is None

        # Set in cache (after querying database)
        cache.set(short_code, original_url, ttl=3600)

        # Second access - cache hit
        cached = cache.get(short_code)
        assert cached == original_url

    def test_cache_deletion_on_url_removal(self, cache):
        """Test cache invalidation when URL is deleted"""
        short_code = "abc123"
        original_url = "https://google.com"

        cache.set(short_code, original_url, ttl=3600)
        assert cache.get(short_code) == original_url

        # Simulate URL deletion (delete from cache)
        cache.delete(short_code)
        assert cache.get(short_code) is None


class TestErrorHandling:
    """Test error handling and edge cases"""

    def test_set_with_zero_ttl(self, cache):
        """Test setting a value with zero TTL (expires immediately in some cases)"""
        cache.set("test_key", "test_value", ttl=0)
        # Behavior depends on Redis - typically expires immediately or is not set
        # This test ensures it doesn't crash

    def test_set_with_negative_ttl(self, cache):
        """Test setting a value with negative TTL"""
        cache.set("test_key", "test_value", ttl=-1)
        # Negative TTL typically means delete immediately
        # This test ensures it doesn't crash

    def test_get_with_empty_string_key(self, cache):
        """Test getting with an empty string key"""
        result = cache.get("")
        assert result is None

    def test_set_empty_string_value(self, cache):
        """Test setting an empty string value"""
        cache.set("empty_key", "", ttl=3600)
        result = cache.get("empty_key")
        assert result == ""


class TestCacheMemoryManagement:
    """Test cache memory and size management"""

    def test_many_keys_in_cache(self, cache):
        """Test storing many keys in cache"""
        # Store 1000 key-value pairs
        for i in range(1000):
            cache.set(f"key_{i}", f"value_{i}", ttl=3600)

        # Verify a few random keys
        assert cache.get("key_0") == "value_0"
        assert cache.get("key_500") == "value_500"
        assert cache.get("key_999") == "value_999"

    def test_cache_keys_retrieval(self, cache):
        """Test retrieving all keys from cache"""
        keys = ["key1", "key2", "key3"]
        for key in keys:
            cache.set(key, f"value_for_{key}", ttl=3600)

        # Retrieve all keys
        all_keys = cache.keys()
        assert set(keys).issubset(set(all_keys))
