"""Employee Attendance & Analytics API. Run: uvicorn app.main:app --port 8000."""
import os
import re
import json
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import ObjectId, Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                     tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]


def create_indexes():
    db.employees.create_index("emp_code", unique=True, name="emp_code_unique")
    db.employees.create_index([("department", 1), ("joined_on", 1), ("emp_code", 1)],
                              name="department_joined")
    db.employees.create_index([("joined_on", 1), ("department", 1)], name="joined_department")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)],
                                    unique=True, name="emp_date_unique")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_employee")
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)],
                                    name="status_date_employee")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="employee_punch")


@asynccontextmanager
async def lifespan(app):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# Input models. Extra fields, including derived values, are ignored.
def valid_date(value):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("expected YYYY-MM-DD")
    Date.fromisoformat(value)
    return value


class InputModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


class EmployeeIn(InputModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    shift_end: str = Field(default="18:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def check_date(cls, value):
        return valid_date(value)

    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchOutIn(InputModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("omit punched_at to use the current time")
        return value


class PunchInIn(PunchOutIn):
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class RegularizeIn(InputModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status", "punch_in", "punch_out", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("omit fields that should remain unchanged")
        return value


# Business rules and boundary conversion.
def invalid(message, field="body", value=None):
    location = (field,) if field == "body" else ("query", field)
    raise RequestValidationError([{"type": "value_error", "loc": location,
                                   "msg": message, "input": value}])


def utc_seconds(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)  # PyMongo naive BSON dates mean UTC
    return value.astimezone(UTC).replace(microsecond=0)


def punch_time(value):
    if value is None:
        return utc_seconds(datetime.now(UTC))
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def json_value(value):
    if isinstance(value, datetime):
        return int(utc_seconds(value).timestamp()) * 1000
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_value(v) for v in value]
    return value


def attendance_json(doc):
    result = {"punch_in": None, "punch_out": None, "work_hours": None,
              "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
    result.update(doc)
    result.pop("_id", None)
    return json_value(result)


def employee_or_404(emp_code):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    return employee


def attendance_date(punch_in, employee):
    local = utc_seconds(punch_in).astimezone(IST)
    day = local.date()
    overnight = employee["shift_end"] <= employee["shift_start"]
    before_shift_end = local.strftime("%H:%M:%S") < employee["shift_end"] + ":00"
    if overnight and before_shift_end:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_instant(day, clock):
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=IST)


def compute_late_minutes(punch_in, shift_start, day=None):
    punch_in = utc_seconds(punch_in)
    day = day or punch_in.astimezone(IST).date().isoformat()
    seconds = int((punch_in - shift_instant(day, shift_start)).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in, punch_out):
    seconds = int((utc_seconds(punch_out) - utc_seconds(punch_in)).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out, shift_end, day, shift_start="00:00"):
    end = shift_instant(day, shift_end)
    if shift_end <= shift_start:
        end += timedelta(days=1)
    minutes = max(0, int((utc_seconds(punch_out) - end).total_seconds() // 60))
    return minutes if minutes >= 30 else 0


def derived_fields(record, employee):
    if record["status"] not in PRESENT_STATUSES:
        return {"punch_in": None, "punch_out": None, "work_hours": None,
                "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    start = record.get("punch_in")
    if start is None:
        invalid("presence status requires punch_in")
    start = utc_seconds(start)
    if attendance_date(start, employee) != record["date"]:
        invalid("punch_in must stay on the attendance date")
    out = record.get("punch_out")
    values = {"punch_in": start, "punch_out": None, "work_hours": None,
              "late_minutes": compute_late_minutes(start, employee["shift_start"], record["date"]),
              "overtime_minutes": 0, "half_day": False}
    if out is not None:
        out = utc_seconds(out)
        seconds = int((out - start).total_seconds())
        if not 0 < seconds <= 86400:
            invalid("punch_out must be after punch_in and within 24 hours")
        hours = compute_work_hours(start, out)
        values["punch_out"] = out
        values["work_hours"] = hours
        values["half_day"] = hours < 4.50
        values["overtime_minutes"] = compute_overtime(
            out, employee["shift_end"], record["date"], employee["shift_start"])
    return values


def snapshot_filter(doc):
    # Compare-and-set rejects writes based on a stale attendance record.
    expected = {"_id": doc["_id"]}
    for key in ("date", "status", "punch_in", "punch_out", "work_hours",
                "late_minutes", "overtime_minutes", "half_day", "history"):
        if key in doc:
            expected[key] = {"$eq": doc[key], "$exists": True}
        else:
            expected[key] = {"$exists": False}
    return expected


def save_attendance(doc, updates, history=None):
    operation = {"$set": updates}
    if history is not None:
        operation["$push"] = {"history": history}
    updated = db.attendance_logs.find_one_and_update(
        snapshot_filter(doc), operation, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "attendance changed; retry the request")
    return attendance_json(updated)


# Employee and attendance endpoints.
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = utc_seconds(datetime.now(UTC))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return json_value({k: v for k, v in doc.items() if k != "_id"})


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    offset = (page - 1) * page_size
    docs = (db.employees.find(query, {"_id": 0})
            .sort("emp_code", 1)
            .skip(offset)
            .limit(page_size))
    return {"items": [json_value(d) for d in docs], "total": db.employees.count_documents(query),
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = employee_or_404(body.emp_code)
    punched_at = punch_time(body.punched_at)
    day = attendance_date(punched_at, employee)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status,
           "punch_in": punched_at, "punch_out": None, "work_hours": None,
           "late_minutes": compute_late_minutes(punched_at, employee["shift_start"], day),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_json(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = employee_or_404(body.emp_code)
    punched_at = punch_time(body.punched_at)
    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code,
         "punch_in": {"$type": "date", "$lt": punched_at + timedelta(seconds=1)}},
        sort=[("punch_in", -1)])
    if doc is None:
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    start = utc_seconds(doc["punch_in"])
    seconds = int((punched_at - start).total_seconds())
    if not 0 < seconds <= 86400:
        invalid("punch_out must be after punch_in and within 24 hours")
    hours = compute_work_hours(start, punched_at)
    # Preserve stored lateness; corrections recompute it.
    updates = {"punch_in": start, "punch_out": punched_at, "work_hours": hours,
               "overtime_minutes": compute_overtime(punched_at, employee["shift_end"],
                                                      doc["date"], employee["shift_start"]),
               "half_day": hours < 4.50}
    return save_attendance(doc, updates)


def checked_date(value, name):
    if value is not None:
        try:
            return valid_date(value)
        except ValueError:
            invalid("expected a valid YYYY-MM-DD date", name, value)
    return None


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    checked_date(date_from, "date_from")
    checked_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        invalid("date_from must not exceed date_to", "date_to", date_to)
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
    return query


def attendance_hint(query):
    if "emp_code" in query:
        return "emp_date_unique"
    return "status_date_employee" if "status" in query else "date_employee"


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[Status] = None,
                    page: Page = 1, page_size: PageSize = 20):
    query = attendance_query(emp_code, date_from, date_to, status)
    docs = (db.attendance_logs.find(query).hint(attendance_hint(query))
            .sort([("date", -1), ("emp_code", 1)])
            .skip((page - 1) * page_size).limit(page_size))
    return {"items": [attendance_json(d) for d in docs],
            "total": db.attendance_logs.count_documents(query, hint=attendance_hint(query)),
            "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    checked_date(date, "date")
    employee = employee_or_404(emp_code)
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if doc is None:
        raise HTTPException(404, "attendance not found")
    supplied = body.model_fields_set
    updated = dict(doc)
    if "status" in supplied:
        updated["status"] = body.status
    if updated["status"] not in PRESENT_STATUSES:
        if supplied & {"punch_in", "punch_out"}:
            invalid("ABSENT or LEAVE cannot have punch times")
    else:
        for key in ("punch_in", "punch_out"):
            if key in supplied:
                updated[key] = punch_time(getattr(body, key))
    updated.update(derived_fields(updated, employee))
    defaults = {"punch_in": None, "punch_out": None, "work_hours": None,
                "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    changes = {}
    for key in ("status", *defaults):
        old = doc.get(key, defaults.get(key))
        new = updated[key]
        if isinstance(old, datetime):
            old = utc_seconds(old)
        if old != new:
            changes[key] = {"from": old, "to": new}
    if not changes:
        invalid("request changes nothing")
    history = {"at": utc_seconds(datetime.now(UTC)), "by": body.regularized_by,
               "reason": body.reason, "changes": changes}
    fields = {key: updated[key] for key in ("status", *defaults)}
    return save_attendance(doc, fields, history)


# Analytics pipeline builders and routes follow below.

def month_bounds(month):
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    # Inclusive last day avoids constructing a date beyond year 9999.
    end = start.replace(day=monthrange(start.year, start.month)[1])
    return start, end


def half_up(expression, places=2):
    """Reported values are nonnegative; Decimal avoids binary float rounding."""
    scale = 10 ** places
    scaled = {"$multiply": [{"$toDecimal": expression}, scale]}
    rounded = {"$floor": {"$add": [scaled, {"$toDecimal": "0.5"}]}}
    result = {"$toDouble": {"$divide": [rounded, scale]}}
    return {"$cond": [{"$eq": [expression, None]}, None, result]}


def presence(weekdays_only=False):
    conditions = [{"$in": ["$status", PRESENT_STATUSES]}]
    if weekdays_only:
        day_of_week = {"$isoDayOfWeek": {"$toDate": "$date"}}
        conditions.append({"$in": [day_of_week, [1, 2, 3, 4, 5]]})
    day_weight = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}
    return {"$cond": [{"$and": conditions}, day_weight, 0]}


def log_totals():
    is_present = {"$in": ["$status", PRESENT_STATUSES]}
    late_minutes = {"$ifNull": ["$late_minutes", 0]}
    work_hours = {"$toDecimal": {"$ifNull": ["$work_hours", 0]}}
    has_work_hours = {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}
    return {"$group": {
        "_id": None,
        "present_days": {"$sum": presence(True)},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [late_minutes, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": late_minutes},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "hours_sum": {"$sum": {"$cond": [is_present, work_hours, 0]}},
        "hours_count": {"$sum": {"$cond": [{"$and": [is_present, has_work_hours]}, 1, 0]}},
    }}


def monthly_logs(start, end):
    match = {
        "date": {"$gte": start.strftime("%Y-%m-%d"), "$lte": end.strftime("%Y-%m-%d")},
        "$expr": {"$eq": ["$emp_code", "$$code"]},
    }
    return {"$lookup": {
        "from": "attendance_logs",
        "let": {"code": "$emp_code"},
        "pipeline": [{"$match": match}, log_totals()],
        "as": "totals",
    }}


def total_value(field):
    return {"$ifNull": [{"$arrayElemAt": [f"$totals.{field}", 0]}, 0]}


def employee_monthly_query(emp_code, month):
    start, end = month_bounds(month)
    # Count weekdays from the later of the joining date and the month's first day.
    days_remaining = {"$dateDiff": {"startDate": "$first_day", "endDate": end, "unit": "day"}}
    calendar_days = {"$max": [0, {"$add": [days_remaining, 1]}]}
    date_at_offset = {"$dateAdd": {"startDate": "$first_day", "unit": "day", "amount": "$$offset"}}
    working_days = {"$size": {"$filter": {
        "input": {"$range": [0, calendar_days]},
        "as": "offset",
        "cond": {"$lte": [{"$isoDayOfWeek": date_at_offset}, 5]},
    }}}
    attendance_ratio = {"$divide": [{"$toDecimal": total_value("present_days")}, "$working_days"]}
    attendance_pct = {"$cond": [
        {"$gt": ["$working_days", 0]},
        half_up({"$multiply": [attendance_ratio, 100]}),
        None,
    ]}
    pipeline = [
        {"$match": {"emp_code": emp_code}},
        monthly_logs(start, end),
        {"$set": {"first_day": {"$max": [start, {"$toDate": "$joined_on"}]}}},
        {"$set": {"working_days": working_days}},
        {"$project": {
            "_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": 1,
            "present_days": total_value("present_days"),
            "leave_days": total_value("leave_days"),
            "late_count": total_value("late_count"),
            "total_late_minutes": total_value("total_late_minutes"),
            "total_overtime_minutes": total_value("total_overtime_minutes"),
            "attendance_pct": attendance_pct,
        }},
    ]
    return "employees", pipeline, "emp_code_unique"


def department_summary_query(month, department=None):
    start, end = month_bounds(month)
    match = {"joined_on": {"$lte": end.strftime("%Y-%m-%d")}}
    if department is not None:
        match["department"] = department
    avg_work_hours = {"$cond": [
        {"$gt": ["$hours_count", 0]},
        half_up({"$divide": ["$hours_sum", "$hours_count"]}),
        None,
    ]}
    pipeline = [
        {"$match": match},
        monthly_logs(start, end),
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": total_value("present_days")},
            "late_count": {"$sum": total_value("late_count")},
            "total_late_minutes": {"$sum": total_value("total_late_minutes")},
            "leave_count": {"$sum": total_value("leave_days")},
            "on_duty_count": {"$sum": total_value("on_duty_count")},
            "hours_sum": {"$sum": total_value("hours_sum")},
            "hours_count": {"$sum": total_value("hours_count")},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
            "avg_work_hours": avg_work_hours,
        }},
        {"$sort": {"department": 1}},
    ]
    hint = "department_joined" if department is not None else "joined_department"
    return "employees", pipeline, hint


def late_leaderboard_query(month, limit=10, department=None):
    start, end = month_bounds(month)
    # Unlike headcount, the leaderboard does not filter by joining date.
    match = {"department": department} if department is not None else {"emp_code": {"$gte": ""}}
    pipeline = [
        {"$match": match},
        monthly_logs(start, end),
        {"$set": {"total_late_minutes": total_value("total_late_minutes"), "late_count": total_value("late_count")}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {
            "_id": 0, "emp_code": 1, "name": 1, "department": 1,
            "total_late_minutes": 1, "late_count": 1, "rank": 1,
        }},
    ]
    hint = "department_joined" if department is not None else "emp_code_unique"
    return "employees", pipeline, hint


def department_trend_query(department, date_from, date_to):
    start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    # Keep the exclusive bound representable at Python's maximum date.
    if date_to == "9999-12-31":
        end = end.replace(hour=23, minute=59, second=59)
    else:
        end += timedelta(days=1)

    headcount = {"$size": {"$filter": {
        "input": {"$ifNull": ["$joined", []]},
        "as": "joined_on",
        "cond": {"$lte": ["$$joined_on", "$date"]},
    }}}
    daily_logs = [
        {"$match": {"$expr": {"$and": [
            {"$eq": ["$date", "$$date"]},
            {"$in": ["$emp_code", "$$codes"]},
        ]}}},
        {"$group": {
            "_id": None,
            "present_count": {"$sum": presence()},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0,
            ]}},
        }},
    ]
    attendance_rate = {"$cond": [
        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
        half_up({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4),
        None,
    ]}
    pipeline = [
        {"$match": {"department": department}},
        # Read department membership once, rather than joining every log separately.
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"},
                    "joined": {"$push": "$joined_on"}}},
        {"$project": {"_id": 0, "day": {"$literal": start}, "codes": 1, "joined": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start, end]}}},
        # Generated days inherit the department membership from the first row.
        {"$fill": {"sortBy": {"day": 1}, "output": {
            "codes": {"method": "locf"}, "joined": {"method": "locf"}}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                  "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]}}},
        {"$set": {"headcount": headcount}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"date": "$date", "codes": {"$ifNull": ["$codes", []]}},
            "pipeline": daily_logs,
            "as": "totals",
        }},
        {"$set": {"present_count": total_value("present_count"), "late_count": total_value("late_count")}},
        {"$set": {"attendance_rate": attendance_rate}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "moving_avg_7d": {"$avg": {"$toDecimal": "$attendance_rate"}, "window": {"documents": [-6, 0]}}
        }}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": half_up("$moving_avg_7d", 4)}},
        {"$sort": {"date": 1}},
    ]
    return "employees", pipeline, "department_joined"


# The same builders and hints serve the API and executionStats explain.
def checked_month(month):
    if month is None or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        invalid("month is required in YYYY-MM format", "month", month)
    try:
        month_bounds(month)
    except (ValueError, OverflowError):
        invalid("invalid calendar month", "month", month)
    return month


def checked_trend(date_from, date_to):
    if date_from is None or date_to is None:
        invalid("from and to are required", "from")
    checked_date(date_from, "from")
    checked_date(date_to, "to")
    days = (Date.fromisoformat(date_to) - Date.fromisoformat(date_from)).days + 1
    if not 1 <= days <= 92:
        invalid("range must contain 1 to 92 calendar days", "to", date_to)


def aggregate_rows(query):
    collection, pipeline, hint = query
    return list(db[collection].aggregate(pipeline, hint=hint))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    checked_month(month)
    rows = aggregate_rows(employee_monthly_query(emp_code, month))
    if not rows:
        raise HTTPException(404, "employee not found")
    return rows[0]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    checked_month(month)
    return {"month": month, "items": aggregate_rows(department_summary_query(month, department))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: Limit = 10, department: Optional[str] = None):
    checked_month(month)
    return {"month": month, "items": aggregate_rows(late_leaderboard_query(month, limit, department))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str = Query(alias="from"),
                     date_to: str = Query(alias="to")):
    checked_trend(date_from, date_to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    return {"department": department,
            "items": aggregate_rows(department_trend_query(department, date_from, date_to))}


ExplainEndpoint = Literal["attendance_list", "employee_monthly", "department_summary",
                          "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: ExplainEndpoint, emp_code: Optional[str] = None,
                     month: Optional[str] = None, department: Optional[str] = None,
                     limit: Limit = 10, date_from: Optional[str] = None,
                     date_to: Optional[str] = None, status: Optional[Status] = None,
                     page: Page = 1, page_size: PageSize = 20,
                     trend_from: Optional[str] = Query(default=None, alias="from"),
                     trend_to: Optional[str] = Query(default=None, alias="to")):
    if endpoint == "attendance_list":
        query = attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command = {"find": collection, "filter": query,
                   "sort": {"date": -1, "emp_code": 1},
                   "skip": (page - 1) * page_size, "limit": page_size,
                   "hint": attendance_hint(query)}
    else:
        if endpoint == "department_trend":
            if department is None:
                invalid("department is required", "department")
            checked_trend(trend_from, trend_to)
            built = department_trend_query(department, trend_from, trend_to)
        else:
            checked_month(month)
            if endpoint == "employee_monthly":
                if emp_code is None:
                    invalid("emp_code is required", "emp_code")
                built = employee_monthly_query(emp_code, month)
            elif endpoint == "department_summary":
                built = department_summary_query(month, department)
            else:
                built = late_leaderboard_query(month, limit, department)
        collection, pipeline, hint = built
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint}
    explain = db.command("explain", command, verbosity="executionStats")
    # Keep the entire raw explain document; Extended JSON handles BSON metadata.
    return {"endpoint": endpoint, "collection": collection,
            "explain": json.loads(json_util.dumps(explain))}
