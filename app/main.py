"""
Employee Attendance & Analytics API
Run: uvicorn app.main:app --port 8000
"""
import os
import re
import json
from contextlib import asynccontextmanager
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE, "ABSENT", "LEAVE")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
@asynccontextmanager
async def lifespan(_: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# ------------------------------- Indexes ---------------------------------- #
def create_indexes():
    """Idempotent indexes supporting uniqueness, list queries and analytics."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING),("emp_code", ASCENDING)])
    # Supports status-filtered attendance listings while preserving the requested sort.
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING),("emp_code", ASCENDING)])


# ------------------------------ Validation -------------------------------- #
def valid_date(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return value


def valid_time(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("time must be HH:MM")
    return value


def epoch_ms_to_datetime(value: int) -> datetime:
    # Inputs are strict integers and validated before this function is called.
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)


def now_utc_truncated() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(value: datetime) -> datetime:
    """PyMongo normally returns naive UTC datetimes; normalize either form."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(as_utc(value).timestamp() * 1000)


def api_record(doc: dict, employee: bool = False) -> dict:
    """Strip Mongo _id and convert all instant fields, including nested history."""
    out = {k: v for k, v in doc.items() if k != "_id"}
    if employee:
        out["created_at"] = epoch_ms(out.get("created_at"))
        return out
    out["punch_in"] = epoch_ms(out.get("punch_in"))
    out["punch_out"] = epoch_ms(out.get("punch_out"))
    out["history"] = out.get("history") or []
    for entry in out["history"]:
        entry["at"] = epoch_ms(entry.get("at"))
        for field in ("punch_in", "punch_out"):
            if field in entry.get("changes", {}):
                change = entry["changes"][field]
                change["from"] = epoch_ms(change.get("from"))
                change["to"] = epoch_ms(change.get("to"))
    out.setdefault("work_hours", None)
    out.setdefault("late_minutes", 0)
    out.setdefault("overtime_minutes", 0)
    out.setdefault("half_day", False)
    return out


def round_half_up(value, places: int = 2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def shift_bounds(attendance_date: str, employee: dict):
    day = date.fromisoformat(attendance_date)
    sh, sm = map(int, employee["shift_start"].split(":"))
    eh, em = map(int, employee["shift_end"].split(":"))
    start = datetime.combine(day, time(sh, sm), tzinfo=IST)
    end_day = day + timedelta(days=1) if employee["shift_end"] <= employee["shift_start"] else day
    end = datetime.combine(end_day, time(eh, em), tzinfo=IST)
    return start.astimezone(UTC), end.astimezone(UTC)


def attendance_day(punch_in: datetime, employee: dict) -> str:
    local = as_utc(punch_in).astimezone(IST)
    day = local.date()
    if employee["shift_end"] <= employee["shift_start"]:
        eh, em = map(int, employee["shift_end"].split(":"))
        if local.time().replace(tzinfo=None) < time(eh, em):
            day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    punch = as_utc(punch_in).replace(microsecond=0)
    h, m = map(int, shift_start.split(":"))
    day = date.fromisoformat(attendance_date)
    start = datetime.combine(day, time(h, m), tzinfo=IST).astimezone(UTC)
    seconds = int((punch - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((as_utc(punch_out).replace(microsecond=0) -
                   as_utc(punch_in).replace(microsecond=0)).total_seconds())
    return float(Decimal(seconds).__truediv__(Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def compute_overtime(punch_out: datetime, employee: dict, attendance_date: str) -> int:
    _, shift_end = shift_bounds(attendance_date, employee)
    seconds = int((as_utc(punch_out).replace(microsecond=0) - shift_end).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= 30 else 0


def compute_derived(employee: dict, attendance_date: str, punch_in: Optional[datetime],punch_out: Optional[datetime], status: str) -> dict:
    if status in ("ABSENT", "LEAVE"):
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, employee["shift_start"], attendance_date) if punch_in else 0
    if punch_out is None or punch_in is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {"work_hours": hours, "late_minutes": late,
            "overtime_minutes": compute_overtime(punch_out, employee, attendance_date),
            "half_day": hours < 4.50}


def require_month(month: str) -> tuple[str, str, date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, detail="month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    try:
        start = date(year, mon, 1)
        end = date(year, mon, monthrange(year, mon)[1])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM") from exc
    return month, start.isoformat(), start, end


def month_end_string(month: str) -> str:
    _, _, _, end = require_month(month)
    return end.isoformat()


def month_log_match(start_str: str, end_str: str, emp_expr="$emp_code"):
    return {"$expr": {"$and": [
        {"$eq": ["$emp_code", "$$emp_code"]},
        {"$gte": ["$date", start_str]}, {"$lte": ["$date", end_str]}
    ]}}


def weekdays_between_expr(start_expr, end_date: date):
    # Counts Monday-Friday in the inclusive interval; performed inside MongoDB.
    return {"$size": {"$filter": {
        "input": {"$map": {
            "input": {"$range": [0, {"$max": [0, {"$add": [
                {"$dateDiff": {"startDate": start_expr,
                               "endDate": datetime.combine(end_date, time.min, tzinfo=UTC),
                               "unit": "day"}}, 1]}]}]},
            "as": "offset",
            "in": {"$dateAdd": {"startDate": start_expr, "unit": "day", "amount": "$$offset"}}
        }},
        "as": "d",
        "cond": {"$and": [{"$gte": [{"$dayOfWeek": "$$d"}, 2]},
                          {"$lte": [{"$dayOfWeek": "$$d"}, 6]}]}
    }}}


# -------------------------------- Models ----------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift(cls, v):
        return valid_time(v)

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v):
        return valid_date(v)

    @model_validator(mode="after")
    def validate_shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an epoch-millisecond integer when supplied")
        return self


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an epoch-millisecond integer when supplied")
        return self


class RegularizeIn(BaseModel):
    # Reject derived fields such as work_hours/late_minutes: only the contract
    # fields may be changed by a regularization request.
    model_config = ConfigDict(extra="forbid")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_null_fields(self):
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status cannot be null when supplied")
        for field in ("punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} must be an epoch-millisecond integer when supplied")
        return self


# ------------------------------- Endpoints --------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_truncated()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return api_record(doc, employee=True)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
                .skip((page - 1) * page_size).limit(page_size))
    return {"items": [api_record(d, employee=True) for d in docs],
            "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc_truncated()
    punched = punched.replace(microsecond=0)
    day = attendance_day(punched, employee)
    derived = compute_derived(employee, day, punched, None, body.status)
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": punched, "punch_out": None, **derived, "history": []
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return api_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc_truncated()
    punched = punched.replace(microsecond=0)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punched}},
        sort=[("punch_in", DESCENDING)]
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record already punched out")
    if punched <= as_utc(record["punch_in"]) or (punched - as_utc(record["punch_in"])) > timedelta(hours=24):
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in and within 24 hours")
    derived = compute_derived(employee, record["date"], record["punch_in"], punched, record["status"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punched, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record already punched out")
    return api_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        if date_from:
            valid_date(date_from)
        if date_to:
            valid_date(date_to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    total = db.attendance_logs.count_documents(query)
    docs = list(db.attendance_logs.find(query, {"_id": 0})
                .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
                .skip((page - 1) * page_size).limit(page_size))
    return {"items": [api_record(d) for d in docs], "total": total,
            "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        valid_date(date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if old is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    status = body.status if body.status is not None else old["status"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="ABSENT/LEAVE cannot include punch times")
        pin = pout = None
    else:
        pin = epoch_ms_to_datetime(body.punch_in) if body.punch_in is not None else old.get("punch_in")
        pout = epoch_ms_to_datetime(body.punch_out) if body.punch_out is not None else old.get("punch_out")
        if pin is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")
        if attendance_day(pin, employee) != date:
            raise HTTPException(status_code=422, detail="punch_in must belong to the attendance date")
        if pout is not None and (pout <= pin or pout - pin > timedelta(hours=24)):
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")
    derived = compute_derived(employee, date, pin, pout, status)
    final = {"status": status, "punch_in": pin, "punch_out": pout, **derived}
    change_fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes",
                     "overtime_minutes", "half_day")
    changes = {}
    for key in change_fields:
        default_values = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}
        before = old.get(key, default_values.get(key))
        after = final.get(key)
        if isinstance(before, datetime):
            before = as_utc(before)
        if isinstance(after, datetime):
            after = as_utc(after)
        if before != after:
            changes[key] = {"from": old.get(key), "to": final.get(key)}
    if not changes:
        raise HTTPException(status_code=422, detail="regularization changes nothing")
    history_entry = {"at": now_utc_truncated(), "by": body.regularized_by,
                     "reason": body.reason, "changes": changes}
    # Compare-and-set all old mutable fields and history; concurrent editors cannot
    # overwrite one another's update or lose an audit entry.
    cas = {"_id": old["_id"], "status": old["status"]}
    for key in ("punch_in", "punch_out", "work_hours", "late_minutes",
                "overtime_minutes", "half_day", "history"):
        if key in old:
            cas[key] = old[key]
        else:
            cas[key] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        cas, {"$set": final, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record changed concurrently; retry")
    return api_record(updated)


# -------------------------- Analytics pipelines --------------------------- #
def employee_monthly_pipeline(emp_code: str, month: str):
    _, start_str, start_day, end_day = require_month(month)
    end_str = end_day.isoformat()
    start_dt = datetime.combine(start_day, time.min, tzinfo=UTC)
    end_dt = datetime.combine(end_day, time.min, tzinfo=UTC)
    effective_start = {"$cond": [
        {"$gt": [{"$dateFromString": {"dateString": "$joined_on"}}, start_dt]},
        {"$dateFromString": {"dateString": "$joined_on"}}, start_dt
    ]}
    working_days = {"$cond": [
        {"$gt": [{"$dateFromString": {"dateString": "$joined_on"}}, end_dt]},
        0, weekdays_between_expr(effective_start, end_day)
    ]}
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {"from": "attendance_logs", "let": {"emp_code": "$emp_code"},
                     "pipeline": [
                         {"$match": {"$expr": {"$and": [
                             {"$eq": ["$emp_code", "$$emp_code"]},
                             {"$gte": ["$date", start_str]}, {"$lte": ["$date", end_str]}
                         ]}}},
                         {"$set": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
                         {"$group": {"_id": None,
                             "present_days": {"$sum": {"$cond": [
                                 {"$and": [{"$in": ["$status", list(PRESENCE)]},
                                           {"$gte": ["$_weekday", 2]}, {"$lte": ["$_weekday", 6]}]},
                                 {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0]}},
                             "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                             "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                             "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                             "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
                         }}
                     ], "as": "stats"}},
        {"$set": {"stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {}]},
                  "working_days": working_days}},
        {"$project": {"_id": 0, "emp_code": 1, "month": {"$literal": month},
            "working_days": 1, "present_days": {"$ifNull": ["$stats.present_days", 0]},
            "leave_days": {"$ifNull": ["$stats.leave_days", 0]},
            "late_count": {"$ifNull": ["$stats.late_count", 0]},
            "total_late_minutes": {"$ifNull": ["$stats.total_late_minutes", 0]},
            "total_overtime_minutes": {"$ifNull": ["$stats.total_overtime_minutes", 0]},
            "attendance_pct": {"$cond": [
                {"$gt": ["$working_days", 0]},
                {"$multiply": [{"$divide": [
                    {"$ifNull": ["$stats.present_days", 0]}, "$working_days"]}, 100]}, None
            ]}}}
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    require_month(month)
    if db.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="employee not found")
    result = list(db.employees.aggregate(employee_monthly_pipeline(emp_code, month)))
    if not result:
        raise HTTPException(status_code=404, detail="employee not found")
    out = result[0]
    out["present_days"] = round_half_up(out["present_days"], 2)
    out["attendance_pct"] = round_half_up(out["attendance_pct"], 4)
    return out


def department_summary_pipeline(month: str, department: Optional[str] = None):
    _, start_str, _, end_day = require_month(month)
    end_str = end_day.isoformat()
    employee_match: dict[str, object] = {"joined_on": {"$lte": end_str}}

    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_str]},
                    {"$lte": ["$date", end_str]}
                ]}}},
                {"$set": {"_weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
                {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", list(PRESENCE)]},
                                  {"$gte": ["$_weekday", 2]}, {"$lte": ["$_weekday", 6]}]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}, 0]}},
                    "work_hours_sum": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", list(PRESENCE)]},
                                  {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
                    "work_hours_count": {"$sum": {"$cond": [
                        {"$and": [{"$in": ["$status", list(PRESENCE)]},
                                  {"$ne": ["$work_hours", None]}]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$cond": [
                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                        {"$ifNull": ["$late_minutes", 0]}, 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}}
                }}
            ], "as": "stats"}},
        {"$set": {"stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {}]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$stats.present_days", 0]}},
            "work_hours_sum": {"$sum": {"$ifNull": ["$stats.work_hours_sum", 0]}},
            "work_hours_count": {"$sum": {"$ifNull": ["$stats.work_hours_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$stats.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$stats.on_duty_count", 0]}}
        }},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]},
                {"$divide": ["$work_hours_sum", "$work_hours_count"]}, None]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
        {"$sort": {"department": 1}}
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    require_month(month)
    items = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    for item in items:
        item["present_days"] = round_half_up(item["present_days"], 2)
        item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None):
    _, start_str, _, end_day = require_month(month)
    end_str = end_day.isoformat()
    pipeline = [
        {"$match": {"date": {"$gte": start_str, "$lte": end_str},
                    "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                              "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id",
            "name": "$employee.name", "department": "$employee.department",
            "total_late_minutes": 1, "late_count": 1}}
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None):
    require_month(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(
        leaderboard_pipeline(month, limit, department)))}


def department_trend_pipeline(department: str, from_date: str, to_date: str):
    start_day = date.fromisoformat(from_date)
    end_day = date.fromisoformat(to_date)
    start_dt = datetime.combine(start_day, time.min, tzinfo=UTC)
    end_exclusive = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=UTC)
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "employees": {"$push": {
            "emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$project": {"_id": 0, "employees": 1,
            "days": {"$map": {"input": {"$range": [0, {"$dateDiff": {
                "startDate": start_dt, "endDate": end_exclusive, "unit": "day"}}]},
                "as": "offset", "in": {"$dateAdd": {
                    "startDate": start_dt, "unit": "day", "amount": "$$offset"}}}}}},
        {"$unwind": "$days"},
        {"$set": {"day_date": "$days", "date": {"$dateToString": {
            "date": "$days", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "is_working_day": {"$and": [
                {"$gte": [{"$dayOfWeek": "$days"}, 2]},
                {"$lte": [{"$dayOfWeek": "$days"}, 6]}]},
            "headcount": {"$size": {"$filter": {"input": "$employees", "as": "e",
                "cond": {"$lte": ["$$e.joined_on", {"$dateToString": {
                    "date": "$days", "format": "%Y-%m-%d", "timezone": "UTC"}}]}}}},
            "codes": {"$map": {"input": "$employees", "as": "e", "in": "$$e.emp_code"}}}},
        {"$lookup": {"from": "attendance_logs", "localField": "codes",
            "foreignField": "emp_code", "let": {"day": "$date"},
            "pipeline": [{"$match": {"$expr": {"$eq": ["$date", "$$day"]}}}],
            "as": "logs"}},
        {"$set": {
            "present_count": {"$reduce": {"input": "$logs", "initialValue": 0,
                "in": {"$add": ["$$value", {"$cond": [
                    {"$in": ["$$this.status", list(PRESENCE)]},
                    {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1.0]}, 0]}]}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l",
                "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}}
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}}},
        # The complete inclusive date domain was generated with $range above,
        # entirely inside MongoDB. There is no Python-side date loop and no need
        # for a second gap-fill stage (which could add an out-of-range boundary).
        {"$setWindowFields": {"sortBy": {"day_date": 1}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": {"$dateToString": {
                "date": "$day_date", "format": "%Y-%m-%d", "timezone": "UTC"}},
            "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"),
                     to_date: str = Query(..., alias="to")):
    try:
        valid_date(from_date)
        valid_date(to_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    start, end = date.fromisoformat(from_date), date.fromisoformat(to_date)
    if end < start:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (end - start).days + 1 > 92:
        raise HTTPException(status_code=422, detail="date range cannot exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="department not found")
    items = list(db.employees.aggregate(department_trend_pipeline(department, from_date, to_date)))
    for item in items:
        item["present_count"] = round_half_up(item.get("present_count", 0), 2)
        item["attendance_rate"] = round_half_up(item.get("attendance_rate"), 4)
        item["moving_avg_7d"] = round_half_up(item.get("moving_avg_7d"), 4)
        item.setdefault("headcount", 0)
        item.setdefault("late_count", 0)
        item.setdefault("is_working_day", False)
    return {"department": department, "items": items}


# ------------------------------- Explain ---------------------------------- #
def explain_find(collection, query, sort=None, skip=0, limit=20):
    cursor = collection.find(query)
    if sort:
        cursor = cursor.sort(sort)
    cursor = cursor.skip(skip).limit(limit)
    return cursor.explain("executionStats")


def explain_aggregate(collection, pipeline):
    command = {"explain": {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}}
    return db.command(command, verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        try:
            if date_from:
                valid_date(date_from)
            if date_to:
                valid_date(date_to)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, detail="date_from must be <= date_to")
        query = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if status is not None:
            query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        raw = explain_find(db.attendance_logs, query,
                           [("date", DESCENDING), ("emp_code", ASCENDING)],
                           (page - 1) * page_size, page_size)
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required")
        require_month(month)
        raw = explain_aggregate(db.employees, employee_monthly_pipeline(emp_code, month))
        collection = "employees"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required")
        require_month(month)
        raw = explain_aggregate(db.employees, department_summary_pipeline(month, department))
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required")
        require_month(month)
        raw = explain_aggregate(db.attendance_logs, leaderboard_pipeline(month, limit, department))
        collection = "attendance_logs"
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, detail="department, from and to are required")
        try:
            valid_date(from_date)
            valid_date(to_date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if date.fromisoformat(to_date) < date.fromisoformat(from_date) or \
                (date.fromisoformat(to_date) - date.fromisoformat(from_date)).days + 1 > 92:
            raise HTTPException(422, detail="invalid trend date range")
        raw = explain_aggregate(db.employees, department_trend_pipeline(department, from_date, to_date))
        collection = "employees"
    else:
        raise HTTPException(status_code=422, detail="unsupported endpoint")
    # Extended JSON makes BSON-specific values in explain output JSON-safe.
    return {"endpoint": endpoint, "collection": collection,
            "explain": json.loads(json_util.dumps(raw))}
