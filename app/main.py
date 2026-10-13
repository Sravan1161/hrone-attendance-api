"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,                  # datetimes come back with a timezone (UTC), not naive
    serverSelectionTimeoutMS=3000,  # fail fast if MongoDB is down
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

def create_indexes():
    # one employee per emp_code
    db.employees.create_index("emp_code", unique=True)
    # one record per employee per day (this also stops the punch-in race)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    # for listing attendance sorted by date desc, then emp_code asc
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.employees.create_index([("department", 1), ("emp_code", 1)])   # department filter + sort


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()   # runs once when the app starts; safe to run again
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def from_millis(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime, cut down to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def to_millis(dt: datetime) -> int:
    """Datetime -> epoch milliseconds (the format the API returns)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def shift_window(punch_in: datetime, shift_start: str, shift_end: str):
    """Return (attendance_date, shift_start_dt, shift_end_dt). The datetimes are IST."""
    local = punch_in.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    overnight = (eh, em) <= (sh, sm)
    day = local.date()
    # overnight shift: a punch-in earlier than shift_end belongs to the previous day (R1)
    if overnight and (local.hour, local.minute, local.second) < (eh, em, 0):
        day = day - timedelta(days=1)
    start_dt = datetime(day.year, day.month, day.day, sh, sm, tzinfo=IST)
    end_day = day + timedelta(days=1) if overnight else day
    end_dt = datetime(end_day.year, end_day.month, end_day.day, eh, em, tzinfo=IST)
    return day.isoformat(), start_dt, end_dt


def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    seconds = int((punch_in - shift_start_dt).total_seconds())
    return seconds // 60 if seconds > 600 else 0          # strictly more than 10:00 (R2)


def compute_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    minutes = int((punch_out - shift_end_dt).total_seconds()) // 60
    return minutes if minutes >= 30 else 0                # at least 30 minutes (R3)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))   # half-up (R4)
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0






    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


def valid_date_str(s: str) -> str:
    """Accept only a real calendar date written as YYYY-MM-DD."""
    if not DATE_RE.fullmatch(s):
        raise ValueError("must be YYYY-MM-DD")
    datetime.strptime(s, "%Y-%m-%d")  # raises ValueError for e.g. 2026-02-31
    return s


def query_date(value: Optional[str], name: str) -> Optional[str]:
    if value is None:
        return None
    try:
        return valid_date_str(value)
    except ValueError:
        raise HTTPException(422, f"{name} must be a valid YYYY-MM-DD date")


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, v: str) -> str:
        return valid_date_str(v)

    @model_validator(mode="after")
    def check_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    # StrictInt rejects floats and strings; the range rejects values that look like seconds
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


# --------------------------------------------------------------------------- #
# Converting MongoDB documents to the API format (epoch milliseconds, no _id)
# --------------------------------------------------------------------------- #
def employee_out(e: dict) -> dict:
    return {
        "emp_code": e["emp_code"],
        "name": e["name"],
        "email": e["email"],
        "department": e["department"],
        "shift_start": e["shift_start"],
        "shift_end": e["shift_end"],
        "joined_on": e["joined_on"],
        "created_at": to_millis(e["created_at"]),
    }


def history_out(h: dict) -> dict:
    changes = {}
    for field, ch in h.get("changes", {}).items():
        if field in ("punch_in", "punch_out"):
            ch = {
                "from": to_millis(ch["from"]) if ch.get("from") else None,
                "to": to_millis(ch["to"]) if ch.get("to") else None,
            }
        changes[field] = ch
    return {"at": to_millis(h["at"]), "by": h["by"], "reason": h["reason"], "changes": changes}


def record_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": to_millis(d["punch_in"]) if d.get("punch_in") else None,
        "punch_out": to_millis(d["punch_out"]) if d.get("punch_out") else None,
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes", 0),
        "overtime_minutes": d.get("overtime_minutes", 0),
        "half_day": d.get("half_day", False),
        "history": [history_out(h) for h in d.get("history", [])],
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)          # the unique index decides, not a prior find()
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [employee_out(e) for e in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = from_millis(body.punched_at) if body.punched_at is not None else now_utc()
    day, start_dt, _ = shift_window(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, start_dt),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)    # unique (emp_code, date) index decides
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    date_from = query_date(date_from, "date_from")
    date_to = query_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = from_millis(body.punched_at) if body.punched_at is not None else now_utc()

    # the employee's most recent record whose punch_in is at or before ts (works across midnight;
    # ABSENT/LEAVE records have no punch_in, so they never match)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in_ts = rec["punch_in"]
    if ts <= punch_in_ts or ts - punch_in_ts > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")

    _, _, end_dt = shift_window(punch_in_ts, emp["shift_start"], emp["shift_end"])
    hours = compute_work_hours(punch_in_ts, ts)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},      # only succeeds if still open: race-safe
        {"$set": {
            "punch_out": ts,
            "work_hours": hours,
            "overtime_minutes": compute_overtime(ts, end_dt),
            "half_day": hours < 4.5,                  # compared on the rounded value (R5)
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:                               # someone else closed it first
        raise HTTPException(409, "already punched out")
    return record_out(updated)
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
