# URL Shortener

![CI](https://github.com/CypherGuy/URL-Shortener/actions/workflows/run-ci.yml/badge.svg)

A production-oriented URL shortener built as a 4-section course in system design fundamentals. Each section deliberately introduces a new layer of architectural complexity - starting from a working monolith, then evolving toward a distributed system. This is Section 1.

**Stack:** FastAPI · PostgreSQL · SQLAlchemy · Docker · GitHub Actions

---

## Section 1 - Monolithic Baseline

All concerns live in a single file (`app/main.py`): database models, API models, route handlers, session management, and business logic. The goal was to build a correct, fully-tested foundation before introducing architectural changes.

```
┌─────────────────────────────────────┐
│              app/main.py            │
│                                     │
│  HTTP Request                       │
│      ↓                              │
│  FastAPI Route Handler              │
│      ↓                              │
│  Pydantic Validation                │
│      ↓                              │
│  SQLAlchemy ORM                     │
│      ↓                              │
│  PostgreSQL                         │
└─────────────────────────────────────┘
```

After completing the implementation, the system was load tested with Locust to establish a performance baseline and identify where it breaks - and why.

---

## API

| Method   | Path                  | Status | Description                                                  |
| -------- | --------------------- | ------ | ------------------------------------------------------------ |
| `POST`   | `/shorten`            | 201    | Accepts a long URL, returns a 10-character base62 short code |
| `GET`    | `/{short_code}`       | 302    | Resolves a short code and redirects to the original URL      |
| `GET`    | `/stats/{short_code}` | 200    | Returns click count, creation time, and original URL         |
| `DELETE` | `/{short_code}`       | 204    | Removes a short code from the database                       |
| `GET`    | `/health`             | 200    | Health check                                                 |

Interactive docs available at `http://localhost:8000/docs` when running.

---

## Running Locally

**Prerequisites:** Docker Desktop

```bash
git clone https://github.com/CypherGuy/URL-Shortener.git
cd URL-Shortener
docker compose up --build
```

This starts two containers:

- `db` - PostgreSQL 15, with a named volume so data persists across restarts
- `app` - FastAPI on port 8000, waits for the database healthcheck before starting

The app is available at `http://localhost:8000`.

To stop and remove everything (including the database volume):

```bash
docker compose down -v
```

---

## Running Tests

Tests use SQLite so no external database is needed:

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
locust -f load_test.py --host=http://localhost:8000
```

Then open `http://localhost:8089` to configure and start the test.

### Results

**100 concurrent users - stable**

| Metric         | Result                       |
| -------------- | ---------------------------- |
| Throughput     | 108 RPS (~9.3M requests/day) |
| Failure rate   | 0%                           |
| Median latency | 8ms                          |
| p99 latency    | 710ms                        |

**500 concurrent users - catastrophic failure**

| Metric               | Result                        |
| -------------------- | ----------------------------- |
| Throughput           | 34.5 RPS (collapsed from 108) |
| Failure rate         | 97%                           |
| Median latency       | 7,800ms                       |
| Max latency observed | 152,934ms (2.5 minutes)       |

The system failed at roughly 100 concurrent users - 10x earlier than expected.

### Root Cause

The bottleneck is database writes. Every `POST /shorten` performs an `INSERT`, and every `GET /{code}` performs an `UPDATE` to increment the click counter. Under concurrent load, both operations compete for the same table, exhausting SQLAlchemy's connection pool (default: 5 connections, max 15 with overflow) and causing cascading lock contention.

**Section 1's Chain of events:**

```
More users → More concurrent writes
           ↓
Connection pool exhausts → Requests queue
           ↓
Queue fills → Timeouts begin
           ↓
System unresponsive → 97% failure rate
```

---

## What's Next - Section 2

Based on the load test findings, Section 2 will focus on:

- **Decoupling click counting** from the redirect path - the write on every `GET` is the most impactful change
- **Redis caching** for `short_code → original_url` lookups, eliminating database hits on redirects entirely
- **Connection pool tuning** - the default of 5 connections (max 15) is insufficient at any meaningful load
- **Read replicas** - separating read and write operations across database instances

Expected outcome: 10–50x throughput improvement, supporting 1,000+ concurrent users.
