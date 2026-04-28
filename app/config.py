import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./test.db")
READ_REPLICA_URL = os.getenv("READ_REPLICA_URL", "sqlite:///./test_replica.db")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
