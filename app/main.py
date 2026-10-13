"""
Employee Attendance & Analytics API
"""
import os
import json
import re
import calendar
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Literal
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, StrictInt
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import json_util

load_dotenv()

# Initialize MongoDB client with timezone awareness
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

# Status definitions and constants
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PunchStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]

MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create indexes idempotently at startup
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index("department")
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    yield
    client.close()

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

# Helpers

def to_epoch_ms(dt: datetime) -> int:
    return int((dt - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(milliseconds=1))

def parse_epoch_ms(value: int, field: str) -> datetime:
    if value < 100_000_000_000 or value > 4_102_444_800_000:
        raise HTTPException(422, f"Invalid epoch milliseconds for {field}")
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(microsecond=0)

def attendance_date_for(ts_ist: datetime, emp: dict) -> date:
    att_date = ts_ist.date()
    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    if shift_end <= shift_start:
        h_end, m_end = map(int, shift_end.split(":"))
        if ts_ist.hour < h_end or (ts_ist.hour == h_end and ts_ist.minute < m_end):
            att_date = att_date - timedelta(days=1)
    return att_date

def compute_late_minutes(punch_in_dt: datetime, shift_start: str, date_str: str) -> int:
    h, m = map(int, shift_start.split(":"))
    shift_start_dt = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    if punch_in_dt <= shift_start_dt:
        return 0
    diff_sec = (punch_in_dt - shift_start_dt).total_seconds()
    if diff_sec <= 600:
        return 0
    return int(diff_sec // 60)

def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    hours_dec = Decimal(int(diff_sec)) / Decimal(3600)
    return float(hours_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def compute_overtime(punch_out_dt: datetime, shift_end: str, date_str: str, shift_start: str) -> int:
    h_end, m_end = map(int, shift_end.split(":"))
    shift_end_dt = datetime.fromisoformat(date_str).replace(hour=h_end, minute=m_end, tzinfo=IST)
    if shift_end <= shift_start:
        shift_end_dt += timedelta(days=1)
    diff = punch_out_dt - shift_end_dt
    minutes = int(diff.total_seconds() // 60)
    return minutes if minutes >= 30 else 0

def is_half_day(work_hours: float) -> bool:
    return work_hours < 4.50

def round_half_up(value, decimals=2):
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP))

# Models

class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r'^EMP\d{4,6}$')
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    shift_end: str = Field(default="18:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    joined_on: str = Field(..., pattern=r'^\d{4}-\d{2}-\d{2}$')

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date in YYYY-MM-DD format")
        return v

class PunchIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: PunchStatus = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

def format_employee(doc: dict) -> dict:
    res = {}
    for k, v in doc.items():
        if k == "_id":
            continue
        elif k == "created_at" and isinstance(v, datetime):
            res[k] = to_epoch_ms(v)
        else:
            res[k] = v
    return res

def format_attendance(doc: dict) -> dict:
    res = {}
    for k, v in doc.items():
        if k == "_id":
            continue
        res[k] = v

    if res.get("punch_in") and isinstance(res["punch_in"], datetime):
        res["punch_in"] = to_epoch_ms(res["punch_in"])
    if res.get("punch_out") and isinstance(res["punch_out"], datetime):
        res["punch_out"] = to_epoch_ms(res["punch_out"])

    if "half_day" not in res or res["half_day"] is None:
        res["half_day"] = False

    raw_history = res.get("history")
    if raw_history is None:
        res["history"] = []
    else:
        formatted_history = []
        for h in raw_history:
            h_copy = dict(h)
            if "at" in h_copy and isinstance(h_copy["at"], datetime):
                h_copy["at"] = to_epoch_ms(h_copy["at"])
            if "changes" in h_copy and isinstance(h_copy["changes"], dict):
                changes_copy = {}
                for k_c, v_c in h_copy["changes"].items():
                    c_dict = dict(v_c)
                    if k_c in ("punch_in", "punch_out"):
                        if c_dict.get("from") and isinstance(c_dict["from"], datetime):
                            c_dict["from"] = to_epoch_ms(c_dict["from"])
                        if c_dict.get("to") and isinstance(c_dict["to"], datetime):
                            c_dict["to"] = to_epoch_ms(c_dict["to"])
                    changes_copy[k_c] = c_dict
                h_copy["changes"] = changes_copy
            formatted_history.append(h_copy)
        res["history"] = formatted_history

    return res

# Builders for Shared Operational and Explain Logic

def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip = (page - 1) * page_size
    sort_dict = {"date": -1, "emp_code": 1}
    sort_list = [("date", DESCENDING), ("emp_code", ASCENDING)]
    return q, sort_dict, sort_list, skip, page_size

def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    return [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}
        }},
        {"$addFields": {
            "is_weekday": {
                "$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]
            }
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$status", PRESENT_STATUSES]},
                            "$is_weekday"
                        ]},
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                        0
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$late_minutes", 0]}
            },
            "total_overtime_minutes": {
                "$sum": {"$ifNull": ["$overtime_minutes", 0]}
            }
        }}
    ]

def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    end_date_str = f"{month}-{last_day:02d}"

    match_stage = {"joined_on": {"$lte": end_date_str}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp_code": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$emp_code", "$$emp_code"]},
                    "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"}
                }},
                {"$addFields": {
                    "is_weekday": {
                        "$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]
                    }
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {
            "path": "$logs",
            "preserveNullAndEmptyArrays": True
        }},
        {"$group": {
            "_id": "$department",
            "headcount_set": {"$addToSet": "$emp_code"},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$logs.status", PRESENT_STATUSES]},
                            {"$eq": ["$logs.is_weekday", True]}
                        ]},
                        {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]},
                        0
                    ]
                }
            },
            "work_hours_array": {
                "$push": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$logs.status", PRESENT_STATUSES]},
                            {"$ne": ["$logs.work_hours", None]}
                        ]},
                        "$logs.work_hours",
                        "$$REMOVE"
                    ]
                }
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
            },
            "leave_count": {
                "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
            },
            "on_duty_count": {
                "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
            }
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$headcount_set"},
            "present_days": 1,
            "avg_work_hours": {"$avg": "$work_hours_array"},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1
        }},
        {"$sort": {"department": 1}}
    ]

def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list:
    pipeline = [
        {"$match": {
            "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
            "late_minutes": {"$gt": 0}
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1
        }},
        {"$sort": {"rank": 1, "emp_code": 1}}
    ])
    return pipeline

def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> list:
    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc)
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$addFields": {"date_dt": from_dt}},
        {"$densify": {
            "field": "date_dt",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_dt + timedelta(days=1)]
            }
        }},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
            "is_working_day": {
                "$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]
            }
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"day": "$date"},
            "pipeline": [
                {"$match": {
                    "department": department,
                    "$expr": {"$lte": ["$joined_on", "$$day"]}
                }}
            ],
            "as": "emps"
        }},
        {"$addFields": {"headcount": {"$size": "$emps"}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$date", "emp_codes": "$emps.emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$date", "$$day"]},
                            {"$in": ["$emp_code", "$$emp_codes"]}
                        ]
                    }
                }}
            ],
            "as": "logs"
        }},
        {"$addFields": {
            "present_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "log",
                        "in": {
                            "$cond": [
                                {"$in": ["$$log.status", PRESENT_STATUSES]},
                                {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1]},
                                0
                            ]
                        }
                    }
                }
            },
            "late_count": {
                "$size": {
                    "$filter": {
                        "input": "$logs",
                        "as": "log",
                        "cond": {"$gt": ["$$log.late_minutes", 0]}
                    }
                }
            }
        }},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date_dt": 1},
            "output": {
                "moving_avg_7d_raw": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]}
                }
            }
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": "$moving_avg_7d_raw"
        }},
        {"$sort": {"date": 1}}
    ]

# Endpoints

@app.get("/")
def home():
    return {"message": "Welcome to Chronos - Employee Attendance and Analytics API"}

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end cannot be the same")

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return format_employee(doc)

@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size))

    return {
        "items": [format_employee(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size
    }

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts = parse_epoch_ms(body.punched_at, "punched_at")
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    ts_ist = ts.astimezone(IST)
    att_date = attendance_date_for(ts_ist, emp)
    d_str = att_date.isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": d_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts_ist, emp.get("shift_start", "09:30"), d_str),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return format_attendance(doc)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    DATE_REGEX = r"^\d{4}-\d{2}-\d{2}$"
    if date_from:
        if not re.match(DATE_REGEX, date_from):
            raise HTTPException(422, "Invalid date_from format; expected YYYY-MM-DD")
        try:
            date.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(422, "Invalid date_from format; expected YYYY-MM-DD")
    if date_to:
        if not re.match(DATE_REGEX, date_to):
            raise HTTPException(422, "Invalid date_to format; expected YYYY-MM-DD")
        try:
            date.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(422, "Invalid date_to format; expected YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from > date_to")

    if status:
        if status not in ["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]:
            raise HTTPException(422, "Invalid status")

    q, sort_dict, sort_list, skip, page_size = build_attendance_list_query(
        emp_code, date_from, date_to, status, page, page_size
    )
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q).sort(sort_list).skip(skip).limit(page_size)
    items = list(cursor)

    return {
        "items": [format_attendance(d) for d in items],
        "total": total,
        "page": page,
        "page_size": page_size
    }

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, request: Request):
    valid = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in valid:
        raise HTTPException(422, "Invalid endpoint")

    params = request.query_params

    if endpoint == "attendance_list":
        emp_code = params.get("emp_code")
        date_from = params.get("date_from")
        date_to = params.get("date_to")
        status = params.get("status")
        page_val = params.get("page", 1)
        page_size_val = params.get("page_size", 20)

        if date_from:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_from):
                raise HTTPException(422, "Invalid date_from format")
            try:
                date.fromisoformat(date_from)
            except ValueError:
                raise HTTPException(422, "Invalid date_from format")
        if date_to:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_to):
                raise HTTPException(422, "Invalid date_to format")
            try:
                date.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(422, "Invalid date_to format")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from > date_to")
        if status and status not in ["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]:
            raise HTTPException(422, "Invalid status")

        try:
            page = int(page_val)
            if page < 1:
                raise HTTPException(422, "page must be >= 1")
        except (ValueError, TypeError):
            raise HTTPException(422, "page must be an integer >= 1")

        try:
            page_size = int(page_size_val)
            if page_size < 1 or page_size > 100:
                raise HTTPException(422, "page_size must be between 1 and 100")
        except (ValueError, TypeError):
            raise HTTPException(422, "page_size must be an integer between 1 and 100")

        q, sort_dict, sort_list, skip, page_size = build_attendance_list_query(
            emp_code, date_from, date_to, status, page, page_size
        )
        raw = db.command("explain", {
            "find": "attendance_logs",
            "filter": q,
            "sort": sort_dict,
            "skip": skip,
            "limit": page_size
        }, verbosity="executionStats")

    elif endpoint == "employee_monthly":
        emp_code = params.get("emp_code", "EMP0000")
        month = params.get("month", "2000-01")
        if not re.match(MONTH, month):
            raise HTTPException(422, "Invalid month format; expected YYYY-MM")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        raw = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    elif endpoint == "department_summary":
        month = params.get("month", "2000-01")
        if not re.match(MONTH, month):
            raise HTTPException(422, "Invalid month format; expected YYYY-MM")
        department = params.get("department")
        pipeline = build_department_summary_pipeline(month, department)
        raw = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    elif endpoint == "late_leaderboard":
        month = params.get("month", "2000-01")
        if not re.match(MONTH, month):
            raise HTTPException(422, "Invalid month format; expected YYYY-MM")
        limit_val = params.get("limit", 10)
        try:
            limit = int(limit_val)
            if limit < 1:
                raise HTTPException(422, "limit must be >= 1")
        except (ValueError, TypeError):
            raise HTTPException(422, "limit must be an integer >= 1")
        department = params.get("department")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        raw = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    elif endpoint == "department_trend":
        department = params.get("department", "Unknown")
        from_date = params.get("from", "2000-01-01")
        to_date = params.get("to", "2000-01-31")
        try:
            from_d = date.fromisoformat(from_date)
            to_d = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(422, "Invalid date format; expected YYYY-MM-DD")
        if to_d < from_d:
            raise HTTPException(422, "to < from")
        if (to_d - from_d).days > 92:
            raise HTTPException(422, "range exceeds 92 days")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        raw = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")

    return json.loads(json_util.dumps(raw))

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    y, m = map(int, month.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    start_date = max(date(y, m, 1), date.fromisoformat(emp["joined_on"]))
    end_date = date(y, m, last_day)

    working_days = 0
    if start_date <= end_date:
        curr = start_date
        while curr <= end_date:
            if curr.weekday() < 5:
                working_days += 1
            curr += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        data = res[0]
    else:
        data = {
            "present_days": 0, "leave_days": 0,
            "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0
        }

    att_pct = (data["present_days"] / working_days * 100) if working_days > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": data["present_days"],
        "leave_days": data["leave_days"],
        "late_count": data["late_count"],
        "total_late_minutes": data["total_late_minutes"],
        "total_overtime_minutes": data["total_overtime_minutes"],
        "attendance_pct": round_half_up(att_pct, 2)
    }

@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH), department: Optional[str] = None):
    pipeline = build_department_summary_pipeline(month, department)
    res = list(db.employees.aggregate(pipeline))
    for d in res:
        d["avg_work_hours"] = round_half_up(d["avg_work_hours"], 2)

    return {"month": month, "items": res}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH),
    limit: int = Query(10, ge=1),
    department: Optional[str] = None
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    res = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": res}

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Unknown department")

    if not re.match(r"^\d{4}-\d{2}-\d{2}$", from_date) or not re.match(r"^\d{4}-\d{2}-\d{2}$", to_date):
        raise HTTPException(422, "Invalid date format; expected YYYY-MM-DD")

    try:
        from_d = date.fromisoformat(from_date)
        to_d = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, "Invalid date format; expected YYYY-MM-DD")

    if to_d < from_d:
        raise HTTPException(422, "to < from")
    if (to_d - from_d).days > 92:
        raise HTTPException(422, "range exceeds 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    res = list(db.employees.aggregate(pipeline))
    for d in res:
        d["attendance_rate"] = round_half_up(d.get("attendance_rate"), 4)
        d["moving_avg_7d"] = round_half_up(d.get("moving_avg_7d"), 4)

    return {"department": department, "items": res}

@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts = parse_epoch_ms(body.punched_at, "punched_at")
    else:
        ts = datetime.now(timezone.utc).replace(microsecond=0)

    record = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "punch_in": {"$ne": None}
    }, sort=[("punch_in", DESCENDING)])

    if not record:
        raise HTTPException(404, "No punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")

    punch_in_dt = record["punch_in"]

    if ts <= punch_in_dt:
        raise HTTPException(422, "punched_at <= punch_in")

    if (ts - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at more than 24 hours after punch_in")

    work_h = compute_work_hours(punch_in_dt, ts)
    ot = compute_overtime(ts, emp.get("shift_end", "18:30"), record["date"], emp.get("shift_start", "09:30"))
    hd = is_half_day(work_h)

    res = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": work_h,
            "overtime_minutes": ot,
            "half_day": hd
        }},
        return_document=ReturnDocument.AFTER
    )

    if not res:
        raise HTTPException(409, "Already punched out")

    return format_attendance(res)

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    for _ in range(3):
        record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
        if not record:
            raise HTTPException(404, "Record not found")

        new_status = body.status if body.status is not None else record.get("status")

        in_ts = record.get("punch_in")
        if body.punch_in is not None:
            in_ts = parse_epoch_ms(body.punch_in, "punch_in")

        out_ts = record.get("punch_out")
        if body.punch_out is not None:
            out_ts = parse_epoch_ms(body.punch_out, "punch_out")

        if new_status in ["ABSENT", "LEAVE"]:
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(422, "Cannot supply punch times with ABSENT/LEAVE")
            in_ts = None
            out_ts = None
        else:
            if in_ts is None:
                raise HTTPException(422, "Presence status requires punch_in")

            ts_ist = in_ts.astimezone(IST)
            att_date = attendance_date_for(ts_ist, emp)

            if att_date.isoformat() != date:
                raise HTTPException(422, "punch_in does not fall on the record's attendance date")

            if out_ts is not None:
                if out_ts <= in_ts:
                    raise HTTPException(422, "punch_out <= punch_in")
                if (out_ts - in_ts).total_seconds() > 24 * 3600:
                    raise HTTPException(422, "punch_out more than 24 hours after punch_in")

        new_work = None
        new_late = 0
        new_ot = 0
        new_hd = False

        if new_status in PRESENT_STATUSES:
            new_late = compute_late_minutes(in_ts.astimezone(IST), emp.get("shift_start", "09:30"), date)
            if out_ts is not None:
                new_work = compute_work_hours(in_ts, out_ts)
                new_ot = compute_overtime(out_ts, emp.get("shift_end", "18:30"), date, emp.get("shift_start", "09:30"))
                new_hd = is_half_day(new_work)

        changes = {}
        def check_change(key, old_v, new_v):
            if old_v != new_v:
                changes[key] = {"from": old_v, "to": new_v}
                return new_v
            return old_v

        set_fields = {}
        set_fields["status"] = check_change("status", record.get("status"), new_status)
        set_fields["punch_in"] = check_change("punch_in", record.get("punch_in"), in_ts)
        set_fields["punch_out"] = check_change("punch_out", record.get("punch_out"), out_ts)
        set_fields["work_hours"] = check_change("work_hours", record.get("work_hours"), new_work)
        set_fields["late_minutes"] = check_change("late_minutes", record.get("late_minutes", 0), new_late)
        set_fields["overtime_minutes"] = check_change("overtime_minutes", record.get("overtime_minutes", 0), new_ot)
        set_fields["half_day"] = check_change("half_day", record.get("half_day", False), new_hd)

        if not changes:
            raise HTTPException(422, "Request changes nothing")

        history_entry = {
            "at": datetime.now(timezone.utc).replace(microsecond=0),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes
        }

        hist = record.get("history")
        if not hist:
            lock_query = {
                "_id": record["_id"],
                "$or": [
                    {"history": {"$exists": False}},
                    {"history": {"$size": 0}}
                ]
            }
        else:
            lock_query = {
                "_id": record["_id"],
                "history": {"$size": len(hist)}
            }

        res = db.attendance_logs.find_one_and_update(
            lock_query,
            {
                "$set": set_fields,
                "$push": {"history": history_entry}
            },
            return_document=ReturnDocument.AFTER
        )
        if res:
            return format_attendance(res)

    raise HTTPException(409, "Concurrent modification conflict")
