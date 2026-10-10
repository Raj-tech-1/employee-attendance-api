# Employee Attendance & Analytics API

![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?logo=fastapi&logoColor=white)
![MongoDB](https://img.shields.io/badge/MongoDB-6.0%2B-47A248?logo=mongodb&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![Production Ready](https://img.shields.io/badge/status-production--ready-success)

A FastAPI service for employee attendance, manual record corrections, and department-level analytics. It uses **PyMongo** to store records in **MongoDB**, **Pydantic v2** for request validation, and **Uvicorn** as the ASGI server.

The API contract and business rules are documented in [openapi.yaml](openapi.yaml); MongoDB document shapes and sample data are described in [DATA_MODEL.md](DATA_MODEL.md). This repository is an assignment-scale service: it does not include authentication or authorization, so deploy it behind appropriate access controls before exposing it to users.

## Key Features & Architectural Highlights

### Business rules R1–R10

- **R1 — Time zone and attendance date:** Shift times and attendance dates use IST (UTC+05:30). Instants are truncated to whole seconds, stored as BSON UTC datetimes, and returned as epoch milliseconds. Overnight shifts assign after-midnight punch-ins before `shift_end` to the prior shift-start date; the shift end is on the following calendar day.
- **R2 — Late minutes:** A punch-in is late only when it is strictly more than 10 minutes after shift start. Late minutes are floored from shift start, not from the end of the grace period.
- **R3 — Overtime:** Overtime counts whole minutes after shift end only when the elapsed overtime is at least 30 minutes.
- **R4 — Work hours:** Work duration is rounded to two decimal places using decimal half-up rounding.
- **R5 — Half day:** After punch-out, `half_day` is true when the rounded `work_hours` is below 4.50; analytics count it as 0.5 present day.
- **R6 — Presence:** `PRESENT`, `WFH`, and `ON_DUTY` count as present. `ABSENT` and `LEAVE` do not.
- **R7 — Working days:** Working days are Monday–Friday, starting from an employee's `joined_on` date. Monthly present-day counts exclude weekends; late and overtime totals can include weekend records.
- **R8 — Rounding:** Reported durations use two-decimal half-up rounding; rates use four-decimal half-up rounding.
- **R9 — Headcount:** Employees count from their join date, even without attendance logs. Employees joining after the period are excluded.
- **R10 — Pagination:** Pages start at 1, page size defaults to 20 and is capped at 100, and totals reflect active filters.

Epoch millisecond inputs are strict integers in the inclusive range `100000000000`–`4102444800000`. Floats, strings, second-based values, and out-of-range values fail request validation with HTTP 422. Derived fields (`work_hours`, `late_minutes`, `overtime_minutes`, and `half_day`) are computed when attendance is written or regularized, stored in MongoDB, and read directly by analytics pipelines.

### Concurrency and data integrity

- A unique compound index on `(emp_code, date)` prevents duplicate punch-ins for an employee's attendance date; duplicate-key errors return HTTP 409.
- Punch-outs use atomic `find_one_and_update` with a `punch_out: null` condition. Concurrent attempts cannot both close the same record.
- Regularization compares the stored history-array length before updating and appending a correction entry. A stale concurrent correction returns HTTP 409 instead of silently overwriting audit history.
- Each successful regularization appends one audit entry containing only fields that actually changed. Punch-in and punch-out do not modify history.

### Analytics pipelines

Monthly employee and department summaries, late leaderboard, and department trend are calculated with MongoDB aggregations. The leaderboard uses `$setWindowFields` with `$rank` for standard competition ranking, so ties share a rank and the next rank is skipped. The department trend builds calendar dates in the aggregation with `$map`, `$range`, `$dateDiff`, and `$dateAdd`, filling days with no logs before calculating its seven-row moving average. Derived metrics are consumed as stored; they are not reconstructed from punch timestamps in analytics.

## API Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | MongoDB readiness check |
| `POST` / `GET` | `/employees` | Create and list employees |
| `POST` | `/attendance/punch-in` | Record an employee punch-in |
| `POST` | `/attendance/punch-out` | Close the most recent eligible punch-in |
| `GET` | `/attendance` | Filter and paginate attendance records |
| `PATCH` | `/attendance/{emp_code}/{date}` | Correct a record and append audit history |
| `GET` | `/analytics/employees/{emp_code}/monthly` | Employee monthly metrics |
| `GET` | `/analytics/departments/summary` | Department summary for a month |
| `GET` | `/analytics/leaderboard/late` | Ranked late-comers for a month |
| `GET` | `/analytics/departments/{department}/trend` | Gap-filled daily department trend |
| `GET` | `/admin/explain/{endpoint}` | MongoDB execution plan with `executionStats` |

See [openapi.yaml](openapi.yaml) for parameters, schemas, and response details.

## Directory Structure

```text
candidate_kit/
├── app/
│   ├── __init__.py              # Python application package
│   └── main.py                  # FastAPI app, MongoDB indexes, routes, and pipelines
├── sample_data/
│   ├── attendance_logs.json     # Example attendance documents (Extended JSON)
│   └── employees.json           # Example employee documents (Extended JSON)
├── Makefile                     # setup, seed, and run targets
├── .gitignore                   # Excludes local environment and editor artifacts
├── .env                         # Local MongoDB configuration (not committed)
├── README.md                    # Project documentation
├── REVIEW.md                    # Review findings and fixes
├── DECISIONS.md                 # Index, concurrency, and scaling decisions
├── DATA_MODEL.md                # MongoDB collections, fields, and sample shapes
├── openapi.yaml                 # API contract and rules R1–R10
├── PROBLEM_STATEMENT.docx       # Original engineering assignment
├── requirements.txt             # Python dependencies
└── sample_seed.py               # Loads sample documents into MongoDB
```

## Indexing & Performance Strategy

`create_indexes()` provisions indexes idempotently during application startup. Existing default index names are retained for overlapping key patterns, allowing the app to start against databases that already have MongoDB-generated indexes.

### `employees`

| Index keys | Purpose |
|---|---|
| `emp_code ASC` (unique) | Enforces the business identifier and supports employee lookup and joins. |
| `department ASC, emp_code ASC` | Supports department-filtered employee listing and stable code ordering. |
| `department ASC, joined_on ASC, emp_code ASC` | Supports department/date eligibility filters used by trend and summary operations. |
| `joined_on ASC` | Supports join-date eligibility scans. |

### `attendance_logs`

| Index keys | Purpose |
|---|---|
| `emp_code ASC, date ASC` (unique) | Enforces one record per employee/date and supports monthly employee queries and natural-key lookups. |
| `date DESC, emp_code ASC` | Matches the default attendance listing sort and supports date-range analytics, including the late leaderboard. |
| `emp_code ASC, date DESC` | Supports employee history queries in reverse date order. |
| `status ASC, date DESC, emp_code ASC` | Supports status-filtered listing in the required date/employee order. |
| `emp_code ASC, punch_in DESC` | Supports selection of an employee's most recent eligible punch-in. |

These indexes provide indexed access paths for the main filtering and sort patterns instead of relying on collection scans or blocking in-memory sorts where the query shape permits. MongoDB's planner remains data- and query-dependent; use `/admin/explain/{endpoint}` to inspect the actual plan on the target database. The explain endpoint returns the database's raw explain output with `executionStats` verbosity.

On startup, the application logs the active index counts for both collections. Index creation requires a reachable MongoDB server and may fail when existing data violates a requested unique constraint; resolve duplicate keys before startup in that case.

## Quickstart & Setup

### Prerequisites

- Python 3.11 or newer
- MongoDB 6.0 or newer (local instance or MongoDB Atlas)
- GNU Make only if using the `make` targets

### Configure MongoDB

Create a local `.env` file in the repository root (it is ignored by Git):

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

`MONGO_URI` and `MONGO_DB` are read at startup. Actual environment variables take precedence over values loaded from `.env`. For Atlas, set `MONGO_URI` to the connection string supplied by Atlas.

### Option A: Makefile

From the repository root:

```bash
make setup
make seed
make run
```

`make setup` installs the dependencies in `requirements.txt`; `make seed` loads the sample data; `make run` starts Uvicorn on port 8000 with reload enabled. **Seeding deletes existing documents** in the `employees` and `attendance_logs` collections before loading sample records, so use it only with a disposable development database.

### Option B: Standard virtual environment

**Windows PowerShell:**

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

**macOS / Linux:**

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

To start without reloading, omit `--reload` from the Uvicorn command. The module's documented launch command is `uvicorn app.main:app --port 8000`.

## Health Verification & Interactive Docs

With the service running locally:

- Readiness probe: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health) — returns `{"status":"ok"}` when MongoDB responds to a ping, or HTTP 503 when the database is unavailable.
- Swagger UI: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- ReDoc: [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)

## Sample cURL Requests

Examples use `curl.exe` (available in current Windows versions as well as Unix-like systems). Replace employee codes and timestamps with values appropriate to the seeded or created data. Timestamps below are epoch milliseconds.

### Punch in

```bash
curl.exe -X POST "http://127.0.0.1:8000/attendance/punch-in" \
  -H "Content-Type: application/json" \
  -d '{"emp_code":"EMP0001","punched_at":1783312500000,"status":"PRESENT"}'
```

### Punch out

```bash
curl.exe -X POST "http://127.0.0.1:8000/attendance/punch-out" \
  -H "Content-Type: application/json" \
  -d '{"emp_code":"EMP0001","punched_at":1783345200000}'
```

### Late leaderboard

```bash
curl.exe "http://127.0.0.1:8000/analytics/leaderboard/late?month=2026-07&limit=10"
```

Add `&department=Engineering` to restrict the ranking to one department.

### Explain attendance listing

```bash
curl.exe "http://127.0.0.1:8000/admin/explain/attendance_list?page=1&page_size=20&date_from=2026-07-01&date_to=2026-07-31"
```

## Error Handling

Request/schema validation failures return HTTP 422 using FastAPI's validation response. Business conflicts such as duplicate employee or attendance records and concurrent updates return HTTP 409. Unknown employees or records return HTTP 404. MongoDB driver errors are handled by a global exception handler and return a sanitized HTTP 500 response; database connectivity failures at `/health` return HTTP 503.

## Further Reading

- [API contract and business rules](openapi.yaml)
- [MongoDB data model](DATA_MODEL.md)
- [Review notes](REVIEW.md)
- [Implementation decisions](DECISIONS.md)