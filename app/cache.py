class RedisCache:
    def __init__(self, redis_client):
        self.redis_client = redis_client

    def get(self, key):
        return self.redis_client.get(key)

    def set(self, key, value, ttl=3600):
        if ttl < 1:
            self.redis_client.set(key, value)
        else:
            self.redis_client.set(key, value, ex=ttl)

    def exists(self, key):
        return self.redis_client.exists(key) == 1

    def delete(self, key):
        self.redis_client.delete(key)

    def increment(self, key):
        self.redis_client.incrby(key, 1)

    def keys(self, pattern="*"):
        return self.redis_client.keys(pattern)

    def get_int(self, key):
        """Returns get as an integer. We use another method to avoid type-hint confusion."""
        value = self.redis_client.get(key)
        return int(value) if value is not None else 0
