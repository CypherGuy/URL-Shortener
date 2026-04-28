# URL Shortener

![CI](https://github.com/CypherGuy/URL-Shortener/actions/workflows/run-ci.yml/badge.svg)

A production-oriented URL shortener built as a multi-section course in System Design fundamentals. Each section deliberately introduces a new layer of architectural complexity, starting from a working monolith, then evolving toward a distributed system. This is Section 3.

**Stack:** FastAPI · PostgreSQL · SQLAlchemy · Redis · Docker · GitHub Actions

---

## Section 3 - Read Replica & Connection Pool Isolation

Section 2 reduced the database bottleneck by caching reads in Redis and moving click tracking off the critical path. At 500 users the system stayed alive with a 2.3% failure rate, but `POST /shorten` was still failing at 5.6% - every URL creation hits PostgreSQL directly with no cache benefit, and writes competed with reads for the same connection pool.

Section 3 addresses this at the database layer by introducing a read replica and isolating connection pools across concerns.

**1. Read/write split**

All read traffic (`GET /{short_code}` cache misses, `GET /stats`) is routed to a replica database via a dedicated `ReplicaSessionDep`. The primary database now handles writes only (`POST /shorten`, `DELETE /{short_code}`), freeing its connection pool exclusively for write operations.

**2. Manual replica sync**

Since this runs locally without PostgreSQL streaming replication, a background thread syncs the primary to the replica every 60 seconds. It compares short codes across both databases, deletes stale rows from the replica, and upserts all primary rows atomically. On AWS this thread would be removed entirely - RDS Read Replicas use WAL streaming and stay in sync within milliseconds automatically.

**3. Four isolated connection pools**

Each concern gets its own engine and connection pool to prevent one operation from starving another:

```
web_engine          → request handlers (writes)
web_replica_engine  → request handlers (reads)
sync_engine         → Redis → primary flush thread (every 30s)
sync_replica_engine → primary → replica sync thread (every 60s)
```

At scale, four engines would be replaced with a single connection to PgBouncer or RDS Proxy, which manages real PostgreSQL connections centrally.

---

## API

| Method   | Path                  | Status | Description                                                                       |
| -------- | --------------------- | ------ | --------------------------------------------------------------------------------- |
| `POST`   | `/shorten`            | 201    | Accepts a long URL, returns a 10-character base62 short code and the original URL |
| `GET`    | `/{short_code}`       | 302    | Resolves a short code and redirects to the original URL                           |
| `GET`    | `/stats/{short_code}` | 200    | Returns click count, creation time, and original URL                              |
| `DELETE` | `/{short_code}`       | 204    | Removes a short code from the database and cache                                  |
| `GET`    | `/health`             | 200    | Health check                                                                      |

Interactive docs available at `http://localhost:8000/docs` when running.

---

## Running Locally

**Prerequisites:** Docker Desktop, Redis, PostgreSQL

```bash
# Start Redis
brew install redis
brew services start redis

# Start PostgreSQL (adjust version as needed)
brew services start postgresql@17

# Start the app
git clone https://github.com/CypherGuy/URL-Shortener.git
cd URL-Shortener
docker compose up --build
```

This starts two containers:

- `db` - PostgreSQL 15, with a named volume so data persists across restarts
- `app` - FastAPI on port 8000, waits for the database healthcheck before starting

Redis and PostgreSQL run on the host machine. Set `READ_REPLICA_URL` in your `.env` to point to a second database for a genuine read/write split.

The app is available at `http://localhost:8000`.

To stop and remove everything (including the database volume):

```bash
docker compose down -v
```

---

## Running Tests

Tests use SQLite and fakeredis so no external dependencies are needed:

```bash
pip install -r requirements.txt
pytest tests/
```

---

## CI Pipeline

Three sequential jobs run on every push:

```
Lint → Test → Docker
```

| Job        | What it does                                                                                                             |
| ---------- | ------------------------------------------------------------------------------------------------------------------------ |
| **Lint**   | Runs `flake8` across `app/` and `tests/`                                                                                 |
| **Test**   | Spins up a Postgres service container, runs the full test suite with `pytest`                                            |
| **Docker** | Builds the image via `docker compose`, polls `/health` until the app responds, then verifies both containers are running |

Each job only starts if the previous one passes.

---

## Load Testing

The system was load tested with Locust using a realistic traffic distribution:

| Endpoint            | Weight | Rationale                           |
| ------------------- | ------ | ----------------------------------- |
| `GET /{code}`       | 70%    | Redirects dominate real-world usage |
| `POST /shorten`     | 20%    | URL creation is less frequent       |
| `GET /stats/{code}` | 10%    | Analytics traffic                   |

To run the load tests yourself:

```bash
pip install locust
locust -f locustfile.py --host=http://localhost:8000
```

Then open `http://localhost:8089` to configure and start the test.

### Results

**100 concurrent users**

| Metric         | Section 1 | Section 2 | Section 3 | Change (S2→S3)    |
| -------------- | --------- | --------- | --------- | ----------------- |
| Throughput     | 108 RPS   | 327.8 RPS | 297.9 RPS | Marginal drop     |
| Failure rate   | 0%        | 0%        | 0%        | -                 |
| Median latency | 8ms       | 3ms       | 3ms       | Stable            |
| POST median    | 8ms       | 40ms      | 5ms       | ✅ 8x faster      |
| GET p99        | 680ms     | 260ms     | 500ms     | ⚠️ Slightly worse |

**500 concurrent users**

| Metric         | Section 1            | Section 2 | Section 3 | Change (S2→S3)           |
| -------------- | -------------------- | --------- | --------- | ------------------------ |
| Throughput     | 34.5 RPS (collapsed) | ~176 RPS  | ~200 RPS  | ✅ Higher peak           |
| Failure rate   | 97%                  | 2.3%      | 2.2%      | ✅ Marginally better     |
| Total requests | ~13k                 | ~52k      | ~95k      | ✅ 2x more (longer test) |
| Median latency | 7,800ms              | 5ms       | 10ms      | ⚠️ Higher locally        |

### Local Test Limitations

The most recent results suffer from local resource contention as much as architectural improvement. Locust (the load generator) and the app run on the same machine, competing for CPU and memory. The manual 60-second replica sync also creates periodic latency spikes visible in the response time graph.

You would get the full benefits of a read replica when the replica is a physically separate machine. That happens in Section 4 on AWS RDS Read Replicas, which have real WAL streaming replication.

---

## What's Next - Section 4

Section 3 establishes the correct architecture locally. Section 4 moves to AWS to validate it under realistic conditions:

- **EC2** - deploy the app on a real server separate from the load generator
- **RDS** - managed PostgreSQL with a native Read Replica (WAL streaming, millisecond lag)
- **ElastiCache** - managed Redis
- **ALB** - Application Load Balancer distributing traffic across multiple EC2 instances
- **CloudWatch** - monitoring, alerting, and observability

---

## Design Decisions

### Why sync over async

FastAPI runs sync endpoints in a threadpool, so concurrent request handling still works without needing `async def`. The hot path is Redis, and async wouldn't really help there. The database is only hit on cache misses and the 30-second background sync - neither are latency-critical. If the remaining DB calls become a bottleneck later on, using `asyncpg` and `AsyncSession` would be the next step.

### Why four engines

Each background thread and each request handler type gets its own connection pool to prevent one concern from starving another. The background replica sync holds connections open for the duration of each cycle - if it shared a pool with web request handlers, a slow sync could exhaust connections and cause request timeouts. Four engines provide hard isolation guarantees. The tradeoff is four connection pools consuming PostgreSQL connections - addressed at scale with PgBouncer or RDS Proxy.

### Why Redis for click counting

Every time the database was hit for a redirect, a click was incremented. Under load, this became a bottleneck because requests kept getting queued, and the queue became bigger over time.

Redis increments are stored in memory and are basically instant, which removes writes from the hot path entirely. In this case, clicks are flushed to Postgres in bulk every 30 seconds. However, there's a tradeoff to this I accepted: up to 30 seconds of click data could be lost if the app crashes between syncs. This is acceptable for an analytics counter, but not acceptable for something like payments.

### Why the replica sync is a full table scan

The manual sync reads all rows from the primary and reconciles them against the replica on every cycle. An incremental approach (only sync rows modified since last run) would require an `updated_at` column and additional bookkeeping. For a learning project with thousands of rows, the full scan is acceptable. At scale this would be unnecessary - real PostgreSQL replication handles it automatically via WAL streaming.

### Why batch sync every 30 seconds / replica sync every 60 seconds

The Redis→primary flush runs every 30 seconds - short enough that click counts are reasonably fresh for stats queries, long enough that DB write overhead is negligible. The replica sync runs every 60 seconds - less frequent because URL data changes far less often than click counts, and the full table scan is more expensive than a click flush.

### Pydantic vs SQLAlchemy models

Pydantic models (`URLRequest`, `URLResponse`, `StatsResponse`) define what enters and leaves the API - they validate request data and shape responses but are never persisted. SQLAlchemy models (`Code`) map to database tables and handle all persistence. Keeping them separate means validation logic and storage logic don't bleed into each other.

---

## License

MIT
