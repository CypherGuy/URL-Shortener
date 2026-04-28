import os
os.environ["DATABASE_URL"] = "sqlite:///./test_primary.db"
os.environ["READ_REPLICA_URL"] = "sqlite:///./test_replica.db"
