# URL Shortener

![CI](https://github.com/CypherGuy/URL-Shortener/actions/workflows/run-ci.yml/badge.svg)

A production-oriented URL shortener built as a multi-section course in System Design fundamentals. Each section deliberately introduces a new layer of architectural complexity, starting from a working monolith, then evolving toward a distributed system. This is Section 2.

**Stack:** FastAPI · PostgreSQL · SQLAlchemy · Redis · Docker · GitHub Actions

---

## Section 2 — Caching & Async Write Decoupling

Section 1 established a working baseline and load tested it to find where it breaks. The system collapsed at around 100 concurrent users with a 97% failure rate. The root cause was database write contention: every redirect performed a synchronous click counter `UPDATE`, and every URL creation performed an `INSERT` — both competing for the same connection pool under load.

Section 2 reduces database pressure without touching the main infrastructure. From the last milestone, I've made three main changes in order to reduce the bottleneck from the database:

**1. Redis cache-aside on redirects**

Every `GET /{short_code}` checks Redis, an in-memory cache, before PostgreSQL. On a cache hit, the redirect is served entirely from memory without touching the database. On a miss, the URL is fetched from PostgreSQL and stored in Redis with a 1-hour TTL. Once another request for the same URL comes through, it's thus a hit if done within an hour.

**2. Async click tracking**

Click increments are removed from the redirect critical path entirely. A `BackgroundTask` increments a Redis counter after the response is sent. Having this means the user never waits for a database write on redirect.

**3. Batch flush to PostgreSQL**

In order to have changes from Redis persist, a background thread runs every 30 seconds which reads all keys beginning with `clicks:*` (These keys track clicks per short_code) from Redis, and writes the updated values back to PostgreSQL. Click counts eventually persist without blocking requests.

```
┌─────────────────────────────────────────────────┐
│                   app/main.py                   │
│                                                 │
│  GET /{code}                                    │
│      ↓                                          │
│  Redis lookup                                   │
│      ↓ cache hit (~80%)    ↓ cache miss (~20%) │
│  Redirect immediately    PostgreSQL lookup      │
│  + background increment   + populate cache      │
│                           + Redirect            │
│                                                 │
│  Background thread (every 30s)                  │
│      Redis clicks:* → PostgreSQL UPDATE         │
└─────────────────────────────────────────────────┘
```

---

## API

| Method   | Path                  | Status | Description                                                  |
| -------- | --------------------- | ------ | ------------------------------------------------------------ |
| `POST`   | `/shorten`            | 201    | Accepts a long URL, returns a 10-character base62 short code and the original URL for confirmation |
| `GET`    | `/{short_code}`       | 302    | Resolves a short code and redirects to the original URL      |
| `GET`    | `/stats/{short_code}` | 200    | Returns click count, creation time, and original URL         |
| `DELETE` | `/{short_code}`       | 204    | Removes a short code from the database and cache             |
| `GET`    | `/health`             | 200    | Health check                                                 |

Interactive docs available at `http://localhost:8000/docs` when running.

---

## Running Locally

**Prerequisites:** Docker Desktop, Redis

```bash
# Start Redis
brew install redis
brew services start redis

# Start the app
git clone https://github.com/CypherGuy/URL-Shortener.git
cd URL-Shortener
docker compose up --build
```

This starts two containers:

- `db` — PostgreSQL 15, with a named volume so data persists across restarts
- `app` — FastAPI on port 8000, waits for the database healthcheck before starting

Redis runs on the host machine (not in Docker) and is accessible at `localhost:6379`.

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

| Metric         | Section 1 | Section 2 | Change      |
| -------------- | --------- | --------- | ----------- |
| Throughput     | 108 RPS   | 327.8 RPS | +3x         |
| Failure rate   | 0%        | 0%        | —           |
| Median latency | 8ms       | 3ms       | 2.7x faster |
| GET p99        | 680ms     | 260ms     | 2.6x faster |

**500 concurrent users**

| Metric         | Section 1            | Section 2 | Change                                             |
| -------------- | -------------------- | --------- | -------------------------------------------------- |
| Throughput     | 34.5 RPS (collapsed) | ~176 RPS  | System survived                                    |
| Failure rate   | 97%                  | 2.3%      | From catastrophic collapse to marginal degradation |
| Median latency | 7,800ms              | 5ms       | 1,560x faster                                      |

Section 1 collapsed entirely before reaching 100 users. Section 2 reached and sustained 500 users with graceful degradation rather than catastrophic failure.

### Remaining Bottleneck

`POST /shorten` carries a 5.6% failure rate at 500 users — the highest of any endpoint. Every URL creation still hits PostgreSQL directly with no cache benefit. Under heavy write load the connection pool exhausts and requests timeout. This is the target for Section 3.

---

## What's Next — Section 3

The remaining bottleneck is write contention on `POST /shorten`. Section 3 will address this at the database layer before moving to infrastructure:

- **Read replicas** — route read traffic to replicas, freeing the primary for writes only
- **AWS deployment** — EC2, RDS, ElastiCache, ALB
- **Horizontal scaling** — multiple app instances behind a load balancer

---

## Design Decisions

### Why sync over async

FastAPI runs sync endpoints in a threadpool, so concurrent request handling still works without `async def`. The hot path is Redis (~0.1ms operations), so async wouldn't meaningfully help there. The database is only hit on cache misses and the 30-second background sync — neither are latency-critical. A deliberate tradeoff, not an oversight. If the remaining DB calls became a bottleneck, switching to `asyncpg` + `AsyncSession` would be the next step.

### Why two engines (`engine` and `sync_engine`)

The background sync thread holds a DB connection open for the duration of each sync cycle. If it shared a pool with the web request handlers, a slow sync (e.g. Postgres under load) could exhaust the pool and starve incoming web requests of connections. Two engines provide a hard isolation guarantee: sync traffic can never affect web request latency regardless of what the sync is doing. The tradeoff is double the connections to Postgres — a known cost, and one that would be addressed with a connection pooler like PgBouncer at scale.

### Why Redis for click counting

Every redirect was hitting the database to increment a click counter. Under load this serialised — Postgres can only process so many concurrent writes, so requests queued behind each other. Redis increments are in-memory and effectively instant, removing writes from the hot path entirely. Clicks are flushed to Postgres in bulk every 30 seconds. The accepted tradeoff: up to 30 seconds of click data could be lost if the app crashes between syncs — acceptable for an analytics counter, not acceptable for payments.

### Pydantic vs SQLAlchemy models

Pydantic models (`URLRequest`, `URLResponse`) define what enters and leaves the API — they validate request data and shape responses but are never persisted. SQLAlchemy models (`Code`) map to database tables and handle all persistence. Keeping them separate means validation logic and storage logic don't bleed into each other.

### Why batch sync every 30 seconds

Short enough that click counts are reasonably fresh for stats queries. Long enough that the DB write overhead is negligible. The interval is arbitrary and tunable — the architecture supports changing it without code changes beyond the constant.
