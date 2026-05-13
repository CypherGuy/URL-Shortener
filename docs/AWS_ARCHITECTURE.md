# AWS Architecture

## Overview

The URL shortener runs on AWS in `eu-west-2` (London) across a horizontally scaled application layer backed by a separated read/write database layer and an in-memory cache.

![Architecture image](docs/ArchitectureImage.png)

## Components

### EC2 — Application Layer

- **Instance type:** t3.small (2 vCPU, 2 GB RAM) × 2
- **OS:** Amazon Linux 2023
- **Runtime:** Python 3.13, uvicorn (single worker per instance)
- **Region:** eu-west-2

Each instance runs a single uvicorn process serving the FastAPI app. The ALB round-robins traffic across both instances. No auto-scaling group — capacity is fixed at 2 instances.

t3.small was chosen over t3.micro after load testing revealed CPU credit exhaustion under sustained load. t3.small earns credits at 24/hour (vs 12/hour) and has a 20% CPU baseline (vs 10%), sustaining burst traffic for longer.

### RDS PostgreSQL — Database Layer

- **Instance type:** db.t4g.micro × 2 (primary + read replica)
- **Engine:** PostgreSQL
- **Max usable connections:** ~87 per instance

Traffic is split across two separate RDS instances:

| Instance     | Handles                                          | Max app connections |
| ------------ | ------------------------------------------------ | ------------------- |
| Primary      | Writes (`/shorten`, `/delete`) + background sync | 64                  |
| Read Replica | Reads (`/redirect`, `/stats`)                    | 60                  |

Separating reads onto the replica keeps the primary free for writes and doubles total database capacity without upgrading instance size.

### ElastiCache — Cache Layer

- **Engine:** Valkey (Redis-compatible)
- **Pattern:** Cache-aside

Three values are cached per short code:

| Key                 | Value             | Set when              |
| ------------------- | ----------------- | --------------------- |
| `{code}`            | `original_url`    | First DB redirect hit |
| `clicks:{code}`     | click count (int) | First DB redirect hit |
| `created_at:{code}` | ISO timestamp     | First DB redirect hit |

Redirect requests return from cache without touching the database. Stats requests return from cache when all three values are present. Click counts are incremented in Redis and flushed to PostgreSQL every 30 seconds by a background thread (`sync_engine`), avoiding a write to the primary on every redirect.

### Application Load Balancer

Routes HTTP traffic to both EC2 instances. Health checks hit `/health` on each instance; unhealthy instances are automatically removed from rotation.

---

## SQLAlchemy Connection Pool Configuration

Each EC2 instance maintains three independent connection pools:

```python
# Primary RDS — writes only
web_engine = make_engine(DATABASE_URL, pool_size=25, max_overflow=5)

# Read Replica — redirects + stats
web_replica_engine = make_engine(READ_REPLICA_URL, pool_size=25, max_overflow=5)

# Primary RDS — background Redis→DB sync (1 connection every 30s)
sync_engine = make_engine(DATABASE_URL, pool_size=2, max_overflow=0)
```

**Connection math across both instances:**

| RDS Instance | Engines                        | Max connections | RDS limit |
| ------------ | ------------------------------ | --------------- | --------- |
| Primary      | `(25+5) + (2+0)` × 2 instances | 64              | ~87       |
| Replica      | `(25+5)` × 2 instances         | 60              | ~87       |

Pool sizes are asymmetric because traffic is ~80% reads and ~20% writes. Giving the replica a large pool prevents read requests from queuing during bursts, while the write pool is sized to handle peak write RPS with burst headroom.

---

## Load Test Results

Tested with [Locust](https://locust.io/) from a local machine against the ALB. Ramp rate: 2 users/second at 500 users, increasing at a constant ratio of 250 users to an extra user/second ramp up ie 1000 users would have a 4 user/second ramp up. Test duration was 8 minutes from start to finish.

> **Note on locustfile evolution:** The 500 and 750 user tests were run with a fixed `original_url` (`https://example.com/test`), meaning `/shorten` requests were cache hits after the first warm-up and never hit the write pool. From the 1000 user test onwards, `original_url` uses a unique `uuid4()` per request, forcing every `/shorten` call through the full DB write path. The 1000 user results are therefore the more honest stress test of the write path.

### 1000 Concurrent Users

All failures were HTTP 502 Bad Gateway — transient ALB overload, not application errors.

| Endpoint            | Requests    | Failures | Failure Rate | P50       | P95         |
| ------------------- | ----------- | -------- | ------------ | --------- | ----------- |
| `GET /{code}`       | 237,691     | 26       | **0.01%**    | 460ms     | 1,600ms     |
| `POST /shorten`     | 68,657      | 10       | **0.01%**    | 640ms     | 2,300ms     |
| `GET /stats/{code}` | 33,590      | 3        | **0.01%**    | 660ms     | 2,300ms     |
| **Aggregated**      | **339,938** | **39**   | **0.01%**    | **500ms** | **1,900ms** |

Average throughput: **734 RPS**.

### 2000 Concurrent Users

All failures were HTTP 502 Bad Gateway — transient ALB overload, not application errors.

| Endpoint            | Requests    | Failures | Failure Rate | P50       | P95         |
| ------------------- | ----------- | -------- | ------------ | --------- | ----------- |
| `GET /{code}`       | 221,879     | 15       | **0.01%**    | 330ms     | 4,300ms     |
| `POST /shorten`     | 64,970      | 11       | **0.02%**    | 420ms     | 6,400ms     |
| `GET /stats/{code}` | 31,736      | 1        | **0.00%**    | 430ms     | 6,400ms     |
| **Aggregated**      | **318,585** | **27**   | **0.01%**    | **360ms** | **5,800ms** |

Average throughput: **715 RPS**. Failure rate stays near zero but P95 latency climbs sharply — the system absorbs load by slowing rather than shedding requests.

### Summary

The system sustains **2000 concurrent users at a 0.008% failure rate** with zero application errors. All failures were transient 502 Bad Gateway responses. P95 latency rises to 5,800ms at 2000 users as the system approaches its RPS ceiling (~715 RPS), degrading gracefully by queuing rather than shedding requests. The P95 latency ceiling at 2000 users is addressable by horizontal scaling via adding a third EC2 instance or upgrading the RDS primary to db.t4g.small or better.

---

## Request Flow

**Redirect (`GET /{code}`):**

1. Check Redis for `{code}` → if hit, return 302 immediately, increment click in Redis (background)
2. On miss: query read replica, cache `url` + `clicks` + `created_at`, return 302

**Stats (`GET /stats/{code}`):**

1. Check Redis for all three values → if all present, return immediately (no DB)
2. On partial/full miss: query read replica, return response

**Shorten (`POST /shorten`):**

1. Validate URL, generate 10-character random code
2. INSERT into primary via `web_engine`, return short URL

**Background sync (every 30s):**

1. Fetch all `clicks:*` keys from Redis in one call
2. Single `SELECT ... WHERE short_code IN (...)` against primary
3. Bulk update click counts, `session.commit()` flushes all UPDATEs in one round-trip
