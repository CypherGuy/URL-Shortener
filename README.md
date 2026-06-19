# URL Shortener

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/CypherGuy/URL-Shortener)

![CI](https://github.com/CypherGuy/URL-Shortener/actions/workflows/run-ci.yml/badge.svg)

A production-oriented URL shortener built as a multi-section course in System Design fundamentals. Each section deliberately introduces a new layer of architectural complexity, starting from a working monolith, then evolving toward a distributed system. This is Section 4.

**Stack:** FastAPI · PostgreSQL · SQLAlchemy · Redis · Docker · GitHub Actions

---

## Section 4 - AWS Deployment & Load Testing

Section 3 proved the architecture was correct in isolation. Section 4 moves everything to AWS to validate it under realistic conditions - with the load generator on a separate machine, real WAL-based replication, and managed infrastructure.

**Infrastructure**

| Component           | Choice                    | Reason                                                                             |
| ------------------- | ------------------------- | ---------------------------------------------------------------------------------- |
| EC2 (×2)            | t3.small                  | Avoids CPU credit exhaustion that caused t3.micro to throttle under sustained load |
| RDS PostgreSQL (×2) | db.t4g.micro              | Primary for writes, read replica for reads - native WAL streaming replication      |
| ElastiCache         | Valkey (Redis-compatible) | Managed cache, same API as local Redis                                             |
| ALB                 | Application Load Balancer | Round-robins traffic across both EC2 instances, health checks on `/health`         |

Each EC2 instance runs a single uvicorn process serving the FastAPI app. Connection pools are sized asymmetrically to match the actual 80/20 read/write traffic split:

```python
web_engine         = make_engine(DATABASE_URL,     pool_size=25, max_overflow=5)  # writes
web_replica_engine = make_engine(READ_REPLICA_URL, pool_size=25, max_overflow=5)  # reads
sync_engine        = make_engine(DATABASE_URL,     pool_size=2,  max_overflow=0)  # background sync
```

---

## Load Testing

Tested with Locust from a local machine against the ALB. Three tests were run at increasing concurrency, with architectural changes applied between them.

> **Note on the locustfile:** The 500-user test used a fixed `original_url` for all `/shorten` requests. From the 1000-user test onwards, each request uses a unique `uuid4()` URL, forcing every `/shorten` to go through the full DB write path. The later tests are therefore the more honest stress test.

---

### 500 Concurrent Users

| Endpoint            | Requests    | Failures | Failure Rate | P50       | P95       |
| ------------------- | ----------- | -------- | ------------ | --------- | --------- |
| `GET /{code}`       | 231,075     | 22       | 0.010%       | 180ms     | 440ms     |
| `POST /shorten`     | 65,928      | 9        | 0.014%       | 240ms     | 600ms     |
| `GET /stats/{code}` | 33,029      | 1        | 0.003%       | 240ms     | 610ms     |
| **Aggregated**      | **330,032** | **32**   | **0.010%**   | **200ms** | **500ms** |

Peak throughput: **~687 RPS**, stable. Failures per second: **0.07**.

**What the graph shows:** RPS climbs steadily and holds. Response times are well-controlled - p50 at 200ms, p95 at 500ms. All 32 failures are `502 Bad Gateway` ALB responses. This is the optimised system at 500 concurrent users: the connection pools are not under pressure, the cache is absorbing the majority of write lookups, and the system has room to spare.

---

### 1000 Concurrent Users - After Adding `/shorten` Cache

**Change applied:** `/shorten` now checks Redis for an existing mapping before hitting the DB. On a cache hit, the write pool is never touched.

| Endpoint            | Requests    | Failures | Failure Rate | P50       | P95         |
| ------------------- | ----------- | -------- | ------------ | --------- | ----------- |
| `GET /{code}`       | 237,691     | 26       | 0.011%       | 460ms     | 1,600ms     |
| `POST /shorten`     | 68,657      | 10       | 0.015%       | 640ms     | 2,300ms     |
| `GET /stats/{code}` | 33,590      | 3        | 0.009%       | 660ms     | 2,300ms     |
| **Aggregated**      | **339,938** | **39**   | **0.011%**   | **500ms** | **1,900ms** |

Peak throughput: **~734 RPS**, stable. Failures per second: **0.08**.

**What the graph shows:** RPS climbs to a peak of ~900 during ramp-up then settles into an oscillating band of ~650–900, averaging around 750. The p50 climbs gradually from near-zero to ~800–1000ms as users increase, with mild oscillation appearing in the latter half of the test — the early signs of the pool filling under sustained load. The p95 shows the periodic spike pattern clearly: sharp rises to ~2,000–4,000ms on a roughly 30-second cadence, caused by the background sync job's bulk UPDATE briefly holding row locks on the primary. Failures stay flat at near-zero throughout.

**Why the improvement is so large:** The cache eliminates write pool pressure almost entirely. Under the fixed-URL locustfile, every Locust worker shortens the same URL - after the first request warms the cache, every subsequent `/shorten` is a Redis lookup (~1ms) instead of a DB write (~180ms). The write pool goes from saturated to nearly idle. This test uses unique UUID URLs to confirm the write path holds up even without cache assistance - failure rate stayed at 0.011%.

All 39 failures are `502 Bad Gateway` - occasional ALB blips during ramp-up, not application errors.

---

### 2000 Concurrent Users - After Adding `/shorten` Cache

**Change applied:** Fixed a crash in the `/shorten` cache-hit path. Under memory pressure, ElastiCache evicts `created_at:{short_code}` while retaining the `original_url → short_code` mapping (which has a 24-hour TTL keeping it "hot"). The code assumed both keys were always present together and called `datetime.fromisoformat('None')`, crashing with HTTP 500. The fix rehydrates from the DB when `created_at` is missing.

| Endpoint            | Requests    | Failures | Failure Rate | P50       | P95         |
| ------------------- | ----------- | -------- | ------------ | --------- | ----------- |
| `GET /{code}`       | 221,879     | 15       | 0.007%       | 330ms     | 4,300ms     |
| `POST /shorten`     | 64,970      | 11       | 0.017%       | 420ms     | 6,400ms     |
| `GET /stats/{code}` | 31,736      | 1        | 0.003%       | 430ms     | 6,400ms     |
| **Aggregated**      | **318,585** | **27**   | **0.008%**   | **360ms** | **5,800ms** |

Peak throughput: **~715 RPS**, stable. Failures per second: **0.06**.

**What the graph shows:** RPS holds at 700–800. Failures stay at zero. But the p50 line develops a sawtooth - oscillating between near-zero and ~3,000ms on a regular cycle. P95 climbs steadily from ~2,000ms to ~7,000ms as the test progresses.

**Why p50 improved while p95 got worse:** At 2,000 users, `created_codes` fills up faster across all Locust workers, meaning more redirect requests hit codes that are already cached. The fast path (Redis, <50ms) accounts for a larger share of total requests - pulling the median down. But the slow path (DB, pool-contended) is much slower because there are twice as many requests competing for the same number of pool connections. This separates the distribution: faster median, much heavier tail.

**Why throughput doesn't scale past ~700–800 RPS:** Doubling users from 1,000 to 2,000 produced no throughput gain - RPS oscillates between ~600 and ~900 rather than climbing, averaging around 715. This is the architectural ceiling of the current setup. The system is bounded by two constraints working together: the total connection pool capacity (60 replica + 64 primary connections across both instances) and the uvicorn thread pool (a single worker per instance dispatches sync handlers to anyio's default thread pool). More concurrent users beyond this point do not add throughput - they lengthen the queue, which is why latency grows while RPS stays bounded. The oscillation within that band corresponds to the same pool fill-and-drain cycle driving the p50 sawtooth: RPS dips when requests are queuing and spikes briefly when the pool clears. The graceful degradation is intentional: requests wait rather than fail.

All 27 failures are `502 Bad Gateway`. Zero HTTP 500s - the bug fix held.

---

### Results Summary

| Users | Failure Rate | P50   | P95     | RPS  | Notes                                          |
| ----- | ------------ | ----- | ------- | ---- | ---------------------------------------------- |
| 500   | 0.010%       | 200ms | 500ms   | ~687 | Comfortable headroom, pools not under pressure |
| 1,000 | 0.011%       | 500ms | 1,900ms | ~734 | Approaching ceiling, still stable              |
| 2,000 | 0.008%       | 360ms | 5,800ms | ~715 | At ceiling - latency climbs, no failures       |

The system's ceiling under the current architecture is ~715 RPS. Scaling past this requires a third EC2 instance, multiple uvicorn workers per instance, or an async DB driver to remove the thread pool constraint.

---

### Graph Patterns Explained

Three patterns appear across the graphs that are worth understanding rather than just observing.

**No matter how many users there are, throughput is capped at around 715 req/s**

Adding users beyond around 1,000 doesn't increase throughput - RPS stays flat while latency climbs. This is due to something called Saturation: there's more connections then there are configured by the pool size, and this combined with the uvicorn thread pool (Each EC2 instance runs a single uvicorn worker) causes requests to wait in a queue once all the pool connections are being used. More users, a longer queue, slower processing. The RPS ceiling is where arrival rate matches the service rate - everything above it just adds wait time, increaing the p values.

**Why the orange line (p50) develops a sawtooth at higher concurrency**

At 2,000 users the connection pool slowly fills to capacity, at which point incoming requests queue and wait. Because DB operations take similar amounts of time, connections are released in batches rather than one by one - the pool drains in a burst, and all queued requests get dispatched at once. The majority of those queued requests are redirect cache hits that go straight to Redis and complete in ~1ms, so the median drops to near zero. The pool then fills again, the queue builds, and the cycle repeats. At 1,000 users the pool never fully saturates so the pattern does not appear and the line stays smooth.

**Why the purple line (p95) spikes on a roughly 30-second cadence**

Every 30 seconds the background sync job wakes up, fetches all `clicks:*` keys from Redis, and issues a bulk UPDATE to the primary RDS instance via `sync_engine`. Although `sync_engine` has its own connection pool and does not share connections with the web engines, the UPDATE transaction briefly holds row locks on the `urls` table. Any concurrent `POST /shorten` request that lands during this window is blocked waiting for those locks to release before it can INSERT. This contention lasts only as long as the sync commit takes, typically under a second, but it is enough to push the 95th percentile up sharply before it recovers. The spike cadence matches the 30-second flush interval exactly.

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

Four sequential jobs run on every push:

```
Lint → Check Requirements → Test → Docker
```

| Job                    | What it does                                                                                                             |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| **Lint**               | Runs `flake8` across `app/` and `tests/`                                                                                 |
| **Check Requirements** | Runs `pip-compile --dry-run requirements.in` to verify `requirements.txt` is in sync with `requirements.in`              |
| **Test**               | Spins up Postgres and Redis service containers, runs the full test suite with `pytest`                                   |
| **Docker**             | Builds the image via `docker compose`, polls `/health` until the app responds, then verifies both containers are running |

Each job only starts if the previous one passes.

---

## Local Load Testing (Sections 1–3)

Prior to AWS deployment, the system was load tested locally with Locust using a realistic traffic distribution:

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

These were the results up until this point with 100 and 500 users.

**100 concurrent users**

| Metric         | Section 1 | Section 2 | Section 3 | Change (S2→S3)    |
| -------------- | --------- | --------- | --------- | ----------------- |
| Throughput     | 108 RPS   | 327.8 RPS | 297.9 RPS | Marginal drop     |
| Failure rate   | 0%        | 0%        | 0%        | -                 |
| Median latency | 8ms       | 3ms       | 3ms       | Stable            |
| POST median    | 8ms       | 40ms      | 5ms       | ✅ 8x faster      |
| GET p99        | 680ms     | 260ms     | 500ms     | ⚠️ Slightly worse |

**500 concurrent users**

| Metric         | Section 1            | Section 2 | Section 3 | Section 4 (AWS) | Change (S3→S4)            |
| -------------- | -------------------- | --------- | --------- | --------------- | ------------------------- |
| Throughput     | 34.5 RPS (collapsed) | ~176 RPS  | ~200 RPS  | ~687 RPS        | ✅ 3.4x higher            |
| Failure rate   | 97%                  | 2.3%      | 2.2%      | 0.010%          | ✅ Near zero              |
| Total requests | ~13k                 | ~52k      | ~95k      | ~330k           | ✅ 3.5x more              |
| Median latency | 7,800ms              | 5ms       | 10ms      | 200ms           | ✅ Honest — no contention |

### Local Test Limitations

The Sections 1–3 results are skewed by local resource contention: Locust and the app ran on the same machine, competing for CPU and memory. The manual 60-second replica sync also caused periodic latency spikes visible in the response time graphs. The artificially low median latencies (3–5ms) reflect localhost networking, not real-world performance.

Section 4 eliminates both issues. Locust runs on a separate machine to the ALB, and RDS Read Replicas use WAL streaming replication, meaning because the replaica receives changes as they come in, there's no collective sync. The AWS results are the accurate baseline.

---

## Design Decisions

### Why sync over async

FastAPI runs sync endpoints in a threadpool, so concurrent request handling still works without needing `async def`. The hot path is Redis, and async wouldn't really help there. The database is only hit on cache misses and the 30-second background sync - neither are latency-critical. If the remaining DB calls become a bottleneck later on, using `asyncpg` and `AsyncSession` would be the next step.

### Why three engines

Each concern gets its own connection pool to prevent one operation from starving another:

```
web_engine          → request handlers (writes)
web_replica_engine  → request handlers (reads)
sync_engine         → Redis → primary flush thread (every 30s)
```

Section 3 used four engines - the fourth (`sync_replica_engine`) was a background thread that manually synced the local replica and the main engine. On AWS, RDS Read Replicas use WAL streaming and stay in sync automatically, meaning that's now redundant. At scale, all three remaining engines would be replaced with a single connection to PgBouncer or RDS Proxy, which manages real PostgreSQL connections centrally.

### Why Redis for click counting

Every time the database was hit for a redirect, a click was incremented. Under load, this became a bottleneck because requests kept getting queued, and the queue became bigger over time.

Redis increments are stored in memory and are basically instant, which removes writes from the hot path entirely. Clicks are flushed to Postgres in bulk every 30 seconds. The tradeoff: up to 30 seconds of click data could be lost if the app crashes between syncs. This is acceptable for an analytics counter, but not for something like payments.

### Why Pydantic and SQLAlchemy models are separate

Pydantic models (`URLRequest`, `URLResponse`, `StatsResponse`) define what enters and leaves the API - they validate request data and shape responses but are never persisted. SQLAlchemy models (`Code`) map to database tables and handle all persistence. Keeping them separate means validation logic and storage logic don't bleed into each other.

---

## License

MIT
