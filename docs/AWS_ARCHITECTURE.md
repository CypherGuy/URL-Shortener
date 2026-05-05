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

Tested with [Locust](https://locust.io/) from a local machine against the ALB. Ramp rate: 2 users/second.

### 500 Concurrent Users

| Endpoint            | Requests    | Failures | Failure Rate | P50       | P95         |
| ------------------- | ----------- | -------- | ------------ | --------- | ----------- |
| `GET /{code}`       | 135,130     | 268      | **0.20%**    | 210ms     | 1,000ms     |
| `POST /shorten`     | 39,042      | 296      | **0.76%**    | 280ms     | 1,400ms     |
| `GET /stats/{code}` | 19,233      | 38       | **0.20%**    | 270ms     | 1,400ms     |
| **Aggregated**      | **193,405** | **602**  | **0.31%**    | **230ms** | **1,300ms** |

Peak throughput: **403 RPS**. Zero failures per second at steady state.

### 750 Concurrent Users

| Endpoint            | Requests    | Failures  | Failure Rate | P50       | P95          |
| ------------------- | ----------- | --------- | ------------ | --------- | ------------ |
| `GET /{code}`       | 77,859      | 614       | **0.79%**    | 170ms     | 16,000ms     |
| `POST /shorten`     | 22,757      | 882       | **3.88%**    | 220ms     | 21,000ms     |
| `GET /stats/{code}` | 11,182      | 98        | **0.88%**    | 210ms     | 22,000ms     |
| **Aggregated**      | **111,798** | **1,594** | **1.43%**    | **190ms** | **16,000ms** |

Peak throughput: **265 RPS**. Degradation is gradual — the system slows under pool pressure rather than failing hard.

### Summary

The system handles **500 concurrent users at a 0.31% failure rate** with sub-300ms median latency across all endpoints. Above 500 users, failure rates rise as connection pool demand approaches capacity, reaching 1.43% at 750 users. The primary bottleneck at the degradation point is read replica pool pressure — addressable by adding a third EC2 instance or upgrading the RDS replica to db.t4g.small.

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
