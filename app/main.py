"""Employee attendance API and MongoDB-backed analytics (contract v2.0)."""

from __future__ import annotations

import calendar
import logging
import os
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
logger = logging.getLogger("attendance")

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
EpochMillis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
AttendanceStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=UTC)
db = client[MONGO_DB]
employees: Collection[dict[str, Any]] = db["employees"]
attendance_logs: Collection[dict[str, Any]] = db["attendance_logs"]


# --- Date, time, and rounding helpers ---
def truncate_to_seconds(value: datetime) -> datetime:
    """Normalize an instant to UTC whole seconds (Rule R1)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def now_utc_seconds() -> datetime:
    """Return the server clock truncated to seconds (Rule R1)."""
    return truncate_to_seconds(datetime.now(UTC))


def epoch_ms_to_datetime_utc(epoch_ms: int) -> datetime:
    """Convert validated epoch milliseconds to a whole-second UTC instant (Rule R1)."""
    return datetime.fromtimestamp(epoch_ms // 1000, tz=UTC)


def datetime_to_epoch_ms(value: datetime | None) -> int | None:
    """Convert a stored instant to epoch milliseconds, truncating to seconds (Rule R1)."""
    if value is None:
        return None
    return int(truncate_to_seconds(value).timestamp()) * 1000


def parse_time_str(value: str) -> time:
    """Parse a validated 24-hour shift time."""
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour, minute)


def round_half_up(value: int | float | Decimal, decimals: int = 2) -> float:
    """Round a reported numeric value using decimal half-up semantics (Rule R8)."""
    quantum = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def is_weekday(value: date) -> bool:
    """Return whether a calendar date is Monday through Friday (Rule R7)."""
    return value.weekday() < 5


def month_date_range(month: str) -> tuple[str, str, int, int, int]:
    """Return inclusive ISO date bounds and calendar components for a month."""
    year, month_number = (int(part) for part in month.split("-"))
    days = calendar.monthrange(year, month_number)[1]
    return f"{month}-01", f"{month}-{days:02d}", year, month_number, days


def calculate_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """Resolve an attendance date in IST, including overnight shifts (Rule R1)."""
    punch_ist = truncate_to_seconds(punch_in_utc).astimezone(IST)
    start_time = parse_time_str(shift_start)
    end_time = parse_time_str(shift_end)
    attendance_day = punch_ist.date()
    if end_time <= start_time and punch_ist.time() < end_time:
        attendance_day -= timedelta(days=1)
    return attendance_day.isoformat()


def calculate_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_day: str) -> int:
    """Apply the strict ten-minute grace period and floor elapsed minutes (Rule R2)."""
    punch_ist = truncate_to_seconds(punch_in_utc).astimezone(IST)
    shift_start_ist = datetime.combine(date.fromisoformat(attendance_day), parse_time_str(shift_start), tzinfo=IST)
    elapsed_seconds = int((punch_ist - shift_start_ist).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds > 600 else 0


def compute_derived_fields(
    punch_in_utc: datetime,
    punch_out_utc: datetime | None,
    shift_start: str,
    shift_end: str,
    attendance_day: str,
) -> tuple[float | None, int, bool]:
    """Calculate and persist derived values at write time (Rules R2-R5)."""
    if punch_out_utc is None:
        return None, 0, False
    punch_in_utc = truncate_to_seconds(punch_in_utc)
    punch_out_utc = truncate_to_seconds(punch_out_utc)
    elapsed_seconds = int((punch_out_utc - punch_in_utc).total_seconds())
    work_hours = round_half_up(Decimal(elapsed_seconds) / Decimal(3600), 2)

    shift_end_date = date.fromisoformat(attendance_day)
    start_time = parse_time_str(shift_start)
    end_time = parse_time_str(shift_end)
    if end_time <= start_time:
        shift_end_date += timedelta(days=1)
    shift_end_ist = datetime.combine(shift_end_date, end_time, tzinfo=IST)
    overtime_seconds = int((punch_out_utc.astimezone(IST) - shift_end_ist).total_seconds())
    overtime_minutes = overtime_seconds // 60 if overtime_seconds >= 1800 else 0
    return work_hours, max(0, overtime_minutes), work_hours < 4.50


# --- Pydantic request schemas ---
class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @field_validator("joined_on", mode="before")
    @classmethod
    def require_iso_date_string(cls, value: Any) -> date:
        if not isinstance(value, str):
            raise ValueError("joined_on must be a YYYY-MM-DD string")
        return date.fromisoformat(value)


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: EpochMillis | None = None
    status: PresenceStatus = "PRESENT"

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, values: Any) -> Any:
        if isinstance(values, dict) and any(key in values and values[key] is None for key in ("punched_at", "status")):
            raise ValueError("punched_at and status cannot be null")
        return values


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: EpochMillis | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_punch(cls, values: Any) -> Any:
        if isinstance(values, dict) and "punched_at" in values and values["punched_at"] is None:
            raise ValueError("punched_at cannot be null")
        return values


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: AttendanceStatus | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_updates(cls, values: Any) -> Any:
        if isinstance(values, dict) and any(
            key in values and values[key] is None for key in ("status", "punch_in", "punch_out")
        ):
            raise ValueError("status, punch_in, and punch_out cannot be null")
        return values


# --- MongoDB index setup and application lifecycle ---
def create_indexes() -> None:
    """Create idempotent indexes used by writes, endpoint filters, and sorts."""
    # Keep MongoDB's canonical generated names for indexes created by the starter app.
    employees.create_index([("emp_code", ASCENDING)], unique=True)
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_employee_code")
    employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)],
        name="department_joined_employee",
    )
    employees.create_index([("joined_on", ASCENDING)])

    attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)])
    attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_status_date_employee",
    )
    attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_employee_punch_in"
    )
    logger.info(
        "MongoDB indexes ready: employees=%d attendance_logs=%d",
        len(employees.index_information()),
        len(attendance_logs.index_information()),
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize indexes before serving requests and close the client on shutdown."""
    create_indexes()
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def pymongo_error_handler(_: Request, exc: PyMongoError) -> JSONResponse:
    """Return a sanitized database failure without exposing driver details or traces."""
    logger.error("MongoDB operation failed: %s", type(exc).__name__)
    return JSONResponse(status_code=500, content={"detail": "Database operation failed"})


# --- API boundary formatting ---
def format_employee_doc(document: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(document)
    result.pop("_id", None)
    if isinstance(result.get("created_at"), datetime):
        result["created_at"] = datetime_to_epoch_ms(result["created_at"])
    if isinstance(result.get("joined_on"), date):
        result["joined_on"] = result["joined_on"].isoformat()
    return result


def format_attendance_doc(document: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(document)
    result.pop("_id", None)
    result["punch_in"] = datetime_to_epoch_ms(result.get("punch_in"))
    result["punch_out"] = datetime_to_epoch_ms(result.get("punch_out"))
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    history: list[dict[str, Any]] = []
    for entry in result.get("history", []):
        formatted = dict(entry)
        formatted["at"] = datetime_to_epoch_ms(formatted.get("at"))
        changes = {}
        for field, values in formatted.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):
                changes[field] = {
                    "from": datetime_to_epoch_ms(values.get("from")),
                    "to": datetime_to_epoch_ms(values.get("to")),
                }
            else:
                changes[field] = values
        formatted["changes"] = changes
        history.append(formatted)
    result["history"] = history
    return result


def build_attendance_filter(
    emp_code: str | None, date_from: date | None, date_to: date | None, status: str | None
) -> dict[str, Any]:
    """Build the shared attendance listing/explain filter."""
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from is not None or date_to is not None:
        bounds: dict[str, str] = {}
        if date_from is not None:
            bounds["$gte"] = date_from.isoformat()
        if date_to is not None:
            bounds["$lte"] = date_to.isoformat()
        query["date"] = bounds
    return query


def employee_monthly_pipeline(emp_code: str, start_day: str, end_day: str) -> list[dict[str, Any]]:
    """Aggregate monthly metrics using persisted derived fields (Rules R5-R8)."""
    present_status = {"$in": ["$status", list(PRESENCE_STATUSES)]}
    weekday = {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 7]]}]}
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_day, "$lte": end_day}}},
        {"$group": {
            "_id": "$emp_code",
            "present_days": {"$sum": {"$cond": [{"$and": [present_status, weekday]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1.0]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, "$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(start_day: str, end_day: str, department: str | None) -> list[dict[str, Any]]:
    """Build department metrics while preserving employees with no logs (Rule R9)."""
    employee_match: dict[str, Any] = {"joined_on": {"$lte": end_day}}
    if department is not None:
        employee_match["department"] = department
    present_status = {"$in": ["$logs.status", list(PRESENCE_STATUSES)]}
    weekday = {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [1, 7]]}]}
    late_value = {"$ifNull": ["$logs.late_minutes", 0]}
    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_day]}, {"$lte": ["$date", end_day]},
            ]}}}],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount_codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [present_status, {"$cond": [weekday, {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]}, 0]}, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [{"$and": [present_status, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
            "work_hours_count": {"$sum": {"$cond": [{"$and": [present_status, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [late_value, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [{"$gt": [late_value, 0]}, late_value, 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]


def late_leaderboard_pipeline(start_day: str, end_day: str, department: str | None, limit: int) -> list[dict[str, Any]]:
    """Build the late leaderboard with standard competition ranking and post-rank cutoff."""
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start_day, "$lte": end_day}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": DESCENDING}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"$expr": {"$lte": ["$rank", limit]}}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ])
    return pipeline


def department_trend_pipeline(department: str, from_day: str, to_day: str) -> list[dict[str, Any]]:
    """Build gap-filled daily metrics and an in-range seven-row window (Rules R7-R9)."""
    start = {"$dateFromString": {"dateString": from_day}}
    end = {"$dateFromString": {"dateString": to_day}}
    return [
        {"$match": {"department": department}},
        {"$group": {
            "_id": None,
            "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}},
            "employee_codes": {"$addToSet": "$emp_code"},
        }},
        {"$project": {"employees": 1, "employee_codes": 1, "dates": {"$map": {
            "input": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": start, "endDate": end, "unit": "day"}}, 1]}]},
            "as": "offset",
            "in": {"$dateToString": {"format": "%Y-%m-%d", "date": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$offset"}}}},
        }}}},
        {"$unwind": "$dates"},
        {"$project": {
            "date": "$dates",
            "employee_codes": 1,
            "headcount": {"$size": {"$filter": {
                "input": "$employees", "as": "employee", "cond": {"$lte": ["$$employee.joined_on", "$dates"]},
            }}},
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$date", "codes": "$employee_codes"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]},
            ]}}}],
            "as": "day_logs",
        }},
        {"$project": {
            "date": 1, "headcount": 1, "day_logs": 1,
            "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
        }},
        {"$project": {
            "date": 1,
            "headcount": 1,
            "is_working_day": {"$and": [{"$gte": ["$day_of_week", 2]}, {"$lte": ["$day_of_week", 6]}]},
            "present_count": {"$reduce": {
                "input": "$day_logs", "initialValue": 0.0, "in": {"$add": ["$$value", {"$cond": [
                    {"$in": ["$$this.status", list(PRESENCE_STATUSES)]},
                    {"$cond": [{"$ifNull": ["$$this.half_day", False]}, 0.5, 1.0]}, 0.0,
                ]}]},
            }},
            "late_count": {"$reduce": {
                "input": "$day_logs", "initialValue": 0, "in": {"$add": ["$$value", {"$cond": [
                    {"$gt": [{"$ifNull": ["$$this.late_minutes", 0]}, 0]}, 1, 0,
                ]}]},
            }},
        }},
        {"$project": {
            "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]}, None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"date": ASCENDING},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
        }},
    ]


# --- Endpoints ---
@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Readiness probe (database ping)."""
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    return {"status": "ok"}


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(request: EmployeeCreate) -> dict[str, Any]:
    """Create an employee; the unique index arbitrates concurrent duplicates."""
    if request.shift_start == request.shift_end:
        raise HTTPException(status_code=422, detail="shift_start and shift_end must differ")
    document = request.model_dump()
    document["joined_on"] = request.joined_on.isoformat()
    document["created_at"] = now_utc_seconds()
    try:
        employees.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail=f"Employee {request.emp_code} already exists") from None
    return format_employee_doc(document)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """List employees in indexed employee-code order (Rule R10)."""
    query = {"department": department} if department is not None else {}
    skip = (page - 1) * page_size
    total = employees.count_documents(query)
    items = [format_employee_doc(item) for item in employees.find(query).sort("emp_code", ASCENDING).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(request: PunchInRequest) -> dict[str, Any]:
    """Create one daily attendance record; the compound unique index arbitrates races."""
    employee = employees.find_one({"emp_code": request.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    punch_in_time = epoch_ms_to_datetime_utc(request.punched_at) if request.punched_at is not None else now_utc_seconds()
    attendance_day = calculate_attendance_date(punch_in_time, employee["shift_start"], employee["shift_end"])
    late_minutes = calculate_late_minutes(punch_in_time, employee["shift_start"], attendance_day)
    document = {
        "emp_code": request.emp_code,
        "date": attendance_day,
        "status": request.status,
        "punch_in": punch_in_time,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        attendance_logs.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail=f"Attendance record already exists for {request.emp_code} on {attendance_day}",
        ) from None
    return format_attendance_doc(document)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(request: PunchOutRequest) -> dict[str, Any]:
    """Atomically close the most recent eligible punch-in (Rules R3-R5)."""
    employee = employees.find_one({"emp_code": request.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    punch_out_time = epoch_ms_to_datetime_utc(request.punched_at) if request.punched_at is not None else now_utc_seconds()
    record = attendance_logs.find_one(
        {"emp_code": request.emp_code, "punch_in": {"$lte": punch_out_time}, "status": {"$in": list(PRESENCE_STATUSES)}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="No punch-in record found")
    punch_in_time = truncate_to_seconds(record["punch_in"])
    elapsed_seconds = (punch_out_time - punch_in_time).total_seconds()
    if elapsed_seconds <= 0 or elapsed_seconds > 86_400:
        raise HTTPException(status_code=422, detail="Punch-out time must be after punch-in and within 24 hours")
    work_hours, overtime_minutes, half_day = compute_derived_fields(
        punch_in_time, punch_out_time, employee["shift_start"], employee["shift_end"], record["date"]
    )
    updated = attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": punch_out_time,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Record was punched out by a concurrent request")
    return format_attendance_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: AttendanceStatus | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """List filtered attendance without loading the collection into memory (Rule R10)."""
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")
    query = build_attendance_filter(emp_code, date_from, date_to, status)
    skip = (page - 1) * page_size
    total = attendance_logs.count_documents(query)
    cursor = attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(item) for item in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    attendance_date: date = Path(..., alias="date"),
    request: RegularizeRequest = Body(...),
) -> dict[str, Any]:
    """Correct a record and append history with optimistic array-length locking."""
    employee = employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    day_key = attendance_date.isoformat()
    record = attendance_logs.find_one({"emp_code": emp_code, "date": day_key})
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    new_status = request.status or record["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if request.punch_in is not None or request.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot supply punch times for ABSENT/LEAVE status")
        new_punch_in = new_punch_out = None
        new_late, new_work, new_overtime, new_half_day = 0, None, 0, False
    else:
        new_punch_in = epoch_ms_to_datetime_utc(request.punch_in) if request.punch_in is not None else record.get("punch_in")
        new_punch_out = epoch_ms_to_datetime_utc(request.punch_out) if request.punch_out is not None else record.get("punch_out")
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires a valid punch_in")
        new_punch_in = truncate_to_seconds(new_punch_in)
        if calculate_attendance_date(new_punch_in, employee["shift_start"], employee["shift_end"]) != day_key:
            raise HTTPException(status_code=422, detail="New punch_in does not belong to the target attendance date")
        if new_punch_out is not None:
            new_punch_out = truncate_to_seconds(new_punch_out)
            elapsed = (new_punch_out - new_punch_in).total_seconds()
            if elapsed <= 0 or elapsed > 86_400:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")
        new_late = calculate_late_minutes(new_punch_in, employee["shift_start"], day_key)
        new_work, new_overtime, new_half_day = compute_derived_fields(
            new_punch_in, new_punch_out, employee["shift_start"], employee["shift_end"], day_key
        )

    old_values = {
        "status": record.get("status"), "punch_in": record.get("punch_in"), "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"), "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0), "half_day": record.get("half_day", False),
    }
    new_values = {
        "status": new_status, "punch_in": new_punch_in, "punch_out": new_punch_out, "work_hours": new_work,
        "late_minutes": new_late, "overtime_minutes": new_overtime, "half_day": new_half_day,
    }
    changes = {
        field: {"from": old_value, "to": new_values[field]}
        for field, old_value in old_values.items()
        if old_value != new_values[field]
    }
    if not changes:
        raise HTTPException(status_code=422, detail="Request resulted in no field changes")

    history_entry = {"at": now_utc_seconds(), "by": request.regularized_by, "reason": request.reason, "changes": changes}
    expected_history_length = len(record.get("history", []))
    updated = attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, expected_history_length]},
        },
        {
            "$set": {
                "status": new_status, "punch_in": new_punch_in, "punch_out": new_punch_out, "work_hours": new_work,
                "late_minutes": new_late, "overtime_minutes": new_overtime, "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="Concurrent regularization detected")
    return format_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
) -> dict[str, Any]:
    """Return monthly attendance using stored derived fields (Rules R5-R8)."""
    employee = employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    start_day, end_day, year, month_number, days = month_date_range(month)
    joined_value = employee["joined_on"]
    joined_day = joined_value.isoformat() if isinstance(joined_value, date) else joined_value
    working_days = sum(
        1 for day_number in range(1, days + 1)
        if f"{month}-{day_number:02d}" >= joined_day and is_weekday(date(year, month_number, day_number))
    )
    results = list(attendance_logs.aggregate(employee_monthly_pipeline(emp_code, start_day, end_day)))
    metrics = results[0] if results else {}
    present_days = float(metrics.get("present_days", 0.0))
    attendance_pct = round_half_up(Decimal(str(present_days)) * 100 / working_days, 4) if working_days else None
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days, "present_days": present_days,
        "leave_days": int(metrics.get("leave_days", 0)), "late_count": int(metrics.get("late_count", 0)),
        "total_late_minutes": int(metrics.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(metrics.get("total_overtime_minutes", 0)), "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
) -> dict[str, Any]:
    """Return department metrics and include eligible employees without logs (Rule R9)."""
    start_day, end_day, *_ = month_date_range(month)
    results = list(employees.aggregate(department_summary_pipeline(start_day, end_day, department)))
    items = []
    for result in results:
        headcount = len(result["headcount_codes"])
        if not headcount:
            continue
        hours_count = result["work_hours_count"]
        average = round_half_up(Decimal(str(result["work_hours_sum"])) / hours_count, 2) if hours_count else None
        items.append({
            "department": result["_id"], "headcount": headcount, "present_days": float(result["present_days"]),
            "avg_work_hours": average, "late_count": int(result["late_count"]),
            "total_late_minutes": int(result["total_late_minutes"]), "leave_count": int(result["leave_count"]),
            "on_duty_count": int(result["on_duty_count"]),
        })
    items.sort(key=lambda item: item["department"])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: str | None = None,
) -> dict[str, Any]:
    """Rank employees with standard competition ties, applying limit to rank ($rank)."""
    start_day, end_day, *_ = month_date_range(month)
    pipeline = late_leaderboard_pipeline(start_day, end_day, department, limit)
    items = list(attendance_logs.aggregate(pipeline))
    items.sort(key=lambda item: (-item["total_late_minutes"], item["emp_code"]))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_day: date = Query(..., alias="from"),
    to_day: date = Query(..., alias="to"),
) -> dict[str, Any]:
    """Return gap-filled daily data and its in-range seven-day moving average."""
    if to_day < from_day:
        raise HTTPException(status_code=422, detail="'to' date must be on or after 'from' date")
    if (to_day - from_day).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="Department not found")
    pipeline = department_trend_pipeline(department, from_day.isoformat(), to_day.isoformat())
    results = list(employees.aggregate(pipeline))
    results.sort(key=lambda result: result["date"])
    items = [{
        "date": result["date"], "is_working_day": result["is_working_day"], "headcount": int(result["headcount"]),
        "present_count": float(result["present_count"]), "late_count": int(result["late_count"]),
        "attendance_rate": round_half_up(result["attendance_rate"], 4) if result["attendance_rate"] is not None else None,
        "moving_avg_7d": round_half_up(result["moving_avg_7d"], 4) if result["moving_avg_7d"] is not None else None,
    } for result in results]
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"] = Path(...),
    emp_code: str | None = None,
    month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: date | None = None,
    date_to: date | None = None,
    status: AttendanceStatus | None = None,
    from_day: date | None = Query(None, alias="from"),
    to_day: date | None = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Explain the named endpoint's same main query or aggregation."""
    if endpoint == "attendance_list":
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")
        command: dict[str, Any] = {
            "find": "attendance_logs", "filter": build_attendance_filter(emp_code, date_from, date_to, status),
            "sort": {"date": DESCENDING, "emp_code": ASCENDING}, "skip": (page - 1) * page_size, "limit": page_size,
        }
        collection_name = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        start_day, end_day, *_ = month_date_range(month)
        command = {"aggregate": "attendance_logs", "pipeline": employee_monthly_pipeline(emp_code, start_day, end_day), "cursor": {}}
        collection_name = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        start_day, end_day, *_ = month_date_range(month)
        command = {"aggregate": "employees", "pipeline": department_summary_pipeline(start_day, end_day, department), "cursor": {}}
        collection_name = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        start_day, end_day, *_ = month_date_range(month)
        command = {
            "aggregate": "attendance_logs", "pipeline": late_leaderboard_pipeline(start_day, end_day, department, limit),
            "cursor": {},
        }
        collection_name = "attendance_logs"
    else:
        if department is None or from_day is None or to_day is None:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        if to_day < from_day or (to_day - from_day).days > 92:
            raise HTTPException(status_code=422, detail="Invalid department trend date range")
        command = {
            "aggregate": "employees",
            "pipeline": department_trend_pipeline(department, from_day.isoformat(), to_day.isoformat()),
            "cursor": {},
        }
        collection_name = "employees"
    explain = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection_name, "explain": jsonable_encoder(explain)}