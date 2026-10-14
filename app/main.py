"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

Single-file FastAPI + PyMongo service. See openapi.yaml for the contract,
DATA_MODEL.md for the stored data, REVIEW.md for the review of the starter code
and DECISIONS.md for the design decisions.
"""
import os
import json
import calendar
import re
from datetime import date as Date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional
from contextlib import asynccontextmanager

from bson import json_util
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
    # status filter on the attendance list, still in list order
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    # punch-out looks up an employee's latest punch_in
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.employees.create_index([("department", 1), ("emp_code", 1)])   # department filter + sort
    # department summary / trend: employees who joined on or before a date
    db.employees.create_index([("joined_on", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])


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


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)


PRESENCE = ("PRESENT", "WFH", "ON_DUTY")


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


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


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    """The filter for GET /attendance; also used by the explain endpoint."""
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
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


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


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    date = query_date(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    # 1. work out the final status and punch times
    new_status = body.status or rec["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_in = new_out = None
    else:
        new_in = from_millis(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = from_millis(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            raise HTTPException(422, "a presence status needs a punch_in")
        day, start_dt, end_dt = shift_window(new_in, emp["shift_start"], emp["shift_end"])
        if day != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    # 2. recompute every derived field from the final values (R2-R5)
    if new_in is None:
        new = {"status": new_status, "punch_in": None, "punch_out": None, "work_hours": None,
               "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        hours = compute_work_hours(new_in, new_out) if new_out is not None else None
        new = {
            "status": new_status,
            "punch_in": new_in,
            "punch_out": new_out,
            "work_hours": hours,
            "late_minutes": compute_late_minutes(new_in, start_dt),
            "overtime_minutes": compute_overtime(new_out, end_dt) if new_out is not None else 0,
            "half_day": hours is not None and hours < 4.5,
        }

    # 3. history entry: only the fields that actually changed
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }
    changes = {k: {"from": old[k], "to": v} for k, v in new.items() if old[k] != v}
    if not any(k in changes for k in ("status", "punch_in", "punch_out")):
        raise HTTPException(422, "request changes nothing")
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # 4. write it, but only if nobody changed the record since we read it
    guard = {"_id": rec["_id"], "punch_out": rec.get("punch_out")}
    guard["history"] = {"$size": len(rec["history"])} if "history" in rec else {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": new, "$push": {"history": entry}},   # $push appends, it never overwrites
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed by someone else; please retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
DAY_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


def round_half_up(value, places: int = 2) -> float:
    """Round half-up (R8). Python's round() is not half-up."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def month_bounds(month: str):
    """First and last day of 'YYYY-MM', as date objects."""
    y, m = int(month[:4]), int(month[5:7])
    return Date(y, m, 1), Date(y, m, calendar.monthrange(y, m)[1])


def count_weekdays(start: Date, end: Date) -> int:
    """Mon-Fri days from start to end, both included (R7)."""
    if end < start:
        return 0
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


# Pieces of aggregation expressions, reused by several analytics endpoints.
# They read the STORED values; nothing is recomputed from punch times.
LATE = {"$ifNull": ["$late_minutes", 0]}
OVERTIME = {"$ifNull": ["$overtime_minutes", 0]}
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}                      # R6
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
PRESENT_WEIGHT = {"$cond": [                                           # R5, R7
    {"$and": [IS_PRESENT, IS_WEEKDAY]},
    {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
    0,
]}


# ---------- Employee monthly ----------
def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PRESENT_WEIGHT},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": LATE},
            "total_overtime_minutes": {"$sum": OVERTIME},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first, last = month_bounds(month)
    start = max(first, Date.fromisoformat(emp["joined_on"]))      # count from joined_on (R7)
    working_days = count_weekdays(start, last)
    stats = next(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)), {})
    present = stats.get("present_days", 0)
    pct = round_half_up(Decimal(str(present)) / working_days * 100) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present),
        "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


# ---------- Department monthly summary ----------
DS_PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]


def department_summary_pipeline(month, department=None):
    year, mon = int(month[:4]), int(month[5:])
    first = f"{month}-01"
    last = f"{month}-{calendar.monthrange(year, mon)[1]:02d}"

    emp_match = {"joined_on": {"$lte": last}}  # R9: joined on or before month end
    if department:
        emp_match["department"] = department

    def total(expr):
        # add up one expression over every attendance log of the employee
        return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expr}}}

    is_presence = {"$in": ["$$l.status", DS_PRESENCE]}
    is_weekday = {"$lte": [
        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, 5]}
    weight = {"$cond": [
        {"$eq": [{"$ifNull": ["$$l.half_day", False]}, True]}, 0.5, 1]}
    has_hours = {"$and": [
        is_presence,
        {"$ne": [{"$ifNull": ["$$l.work_hours", None]}, None]}]}
    late_min = {"$ifNull": ["$$l.late_minutes", 0]}

    return [
        {"$match": emp_match},
        # employees with no logs keep an empty "logs" array, so they still count (R9)
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",          # served by the (emp_code, date) index
            "pipeline": [{"$match": {"date": {"$gte": first, "$lte": last}}}],
            "as": "logs",
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": total(
                {"$cond": [{"$and": [is_presence, is_weekday]}, weight, 0]})},
            "hundredths": {"$sum": total(
                {"$cond": [has_hours,
                           {"$round": [{"$multiply": ["$$l.work_hours", 100]}, 0]},
                           0]})},
            "hours_count": {"$sum": total({"$cond": [has_hours, 1, 0]})},
            "late_count": {"$sum": total(
                {"$cond": [{"$gt": [late_min, 0]}, 1, 0]})},
            "total_late_minutes": {"$sum": total(late_min)},
            "leave_count": {"$sum": total(
                {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]})},
            "on_duty_count": {"$sum": total(
                {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]})},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_PATTERN),
    department: Optional[str] = Query(None, min_length=1, max_length=50),
):
    rows = list(db.employees.aggregate(department_summary_pipeline(month, department)))
    out = []
    for r in rows:
        n = r["hours_count"]
        if n:
            avg = (Decimal(int(r["hundredths"])) / Decimal(n) / Decimal(100)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            avg = Decimal("0.00")
        out.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": float(r["present_days"]),
            "avg_work_hours": float(avg),
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        })
    return out


# ---------- Late leaderboard ----------
def late_leaderboard_pipeline(month, limit, department=None):
    year, mon = int(month[:4]), int(month[5:])
    first = f"{month}-01"
    last = f"{month}-{calendar.monthrange(year, mon)[1]:02d}"

    pipeline = [
        # only late logs in the month; the (date, emp_code) index serves this
        {"$match": {"date": {"$gte": first, "$lte": last},
                    "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist are dropped
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # standard competition ranking: ties share a rank (1, 2, 2, 4)
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},  # keeps everyone tied at the cutoff
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None, min_length=1, max_length=50),
):
    return list(db.attendance_logs.aggregate(
        late_leaderboard_pipeline(month, limit, department)))


# ---------- Department daily trend ----------
def department_trend_pipeline(codes, joined, date_from, date_to):
    lo = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    hi = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    is_presence = {"$in": ["$status", DS_PRESENCE]}
    weight = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
    win = {"documents": [-6, 0]}  # current row + up to 6 previous rows

    return [
        {"$match": {"date": {"$gte": date_from, "$lte": date_to},
                    "emp_code": {"$in": codes}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [is_presence, weight, 0]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$addFields": {"_d": {"$dateFromString": {"dateString": "$_id"}}}},
        # one row for every calendar day in the range, even with no logs
        {"$densify": {"field": "_d",
                      "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$addFields": {
            "day": {"$dateToString": {"date": "$_d", "format": "%Y-%m-%d"}},
            "dow": {"$isoDayOfWeek": "$_d"},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$addFields": {
            "is_working_day": {"$lte": ["$dow", 5]},
            "headcount": {"$size": {"$filter": {
                "input": {"$literal": joined}, "as": "j",
                "cond": {"$lte": ["$$j", "$day"]}}}},
        }},
        # rate in units of 0.0001, half-up, using integer maths:
        # floor((40000*present + 2*headcount) / (4*headcount))
        {"$addFields": {"rate_u": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": ["$present_count", 40000]},
                          {"$multiply": ["$headcount", 2]}]},
                {"$multiply": ["$headcount", 4]}]}},
            None]}}},
        {"$addFields": {"has_rate": {"$cond": [{"$eq": ["$rate_u", None]}, 0, 1]}}},
        {"$setWindowFields": {
            "sortBy": {"_d": 1},
            "output": {
                "ru_sum": {"$sum": "$rate_u", "window": win},
                "ru_cnt": {"$sum": "$has_rate", "window": win},
            },
        }},
        {"$sort": {"_d": 1}},
        {"$project": {
            "_id": 0,
            "date": "$day",
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": {"$cond": [
                {"$eq": ["$rate_u", None]}, None, {"$divide": ["$rate_u", 10000]}]},
            # mean of the rates, half-up to 4 decimals, again with integer maths
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$ru_cnt", 0]}, None,
                {"$divide": [{"$floor": {"$divide": [
                    {"$add": [{"$multiply": ["$ru_sum", 2]}, "$ru_cnt"]},
                    {"$multiply": ["$ru_cnt", 2]}]}}, 10000]}]},
        }},
    ]


def build_trend_pipeline(department, date_from, date_to):
    """Validate the inputs and build the trend pipeline (used by the endpoint and by explain)."""
    try:
        d1 = Date.fromisoformat(date_from)
        d2 = Date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid date")
    if d2 < d1:
        raise HTTPException(status_code=422, detail="to must not be before from")
    if (d2 - d1).days + 1 > 92:
        raise HTTPException(status_code=422, detail="range must be at most 92 days")

    emps = list(db.employees.find({"department": department},
                                  {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(status_code=404, detail="department not found")
    codes = [e["emp_code"] for e in emps]
    joined = [e["joined_on"] for e in emps]
    return department_trend_pipeline(codes, joined, date_from, date_to)


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: str = Query(..., alias="from", pattern=DAY_PATTERN),
    date_to: str = Query(..., alias="to", pattern=DAY_PATTERN),
):
    return list(db.attendance_logs.aggregate(
        build_trend_pipeline(department, date_from, date_to)))


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
def run_explain(command: dict) -> dict:
    result = db.command({"explain": command, "verbosity": "executionStats"})
    # explain output contains BSON types; convert them to plain JSON
    return json.loads(json_util.dumps(result))


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = Query(None, min_length=1, max_length=50),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    trend_from: Optional[str] = Query(None, alias="from", pattern=DAY_PATTERN),
    trend_to: Optional[str] = Query(None, alias="to", pattern=DAY_PATTERN),
):
    def need(**params):
        missing = [k for k, v in params.items() if v is None]
        if missing:
            raise HTTPException(status_code=422,
                                detail="missing query parameter(s): " + ", ".join(missing))

    def aggregate_command(collection, pipeline):
        return collection, {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": build_attendance_query(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        need(emp_code=emp_code, month=month)
        collection, command = aggregate_command(
            "attendance_logs", employee_monthly_pipeline(emp_code, month))
    elif endpoint == "department_summary":
        need(month=month)
        collection, command = aggregate_command(
            "employees", department_summary_pipeline(month, department))
    elif endpoint == "late_leaderboard":
        need(month=month)
        collection, command = aggregate_command(
            "attendance_logs", late_leaderboard_pipeline(month, limit, department))
    elif endpoint == "department_trend":
        need(department=department, **{"from": trend_from, "to": trend_to})
        collection, command = aggregate_command(
            "attendance_logs", build_trend_pipeline(department, trend_from, trend_to))
    else:
        raise HTTPException(status_code=404, detail="unknown endpoint")

    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(command)}