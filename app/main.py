"""
Employee Attendance & Analytics API - STARTER
"""
import os
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from decimal import Decimal, ROUND_HALF_UP
from bson import json_util

# fixed that BSON TimeStamps to JSON using the json_util.dumps() imported the json_util
# line changed : 288,305,345,363,389,393

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def startup_db_client():
    # Create indexes idempotently at startup
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index("department")

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

# Helpers

def to_epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)

def compute_late_minutes(punch_in_dt: datetime, shift_start: str, date_str: str) -> int:
    h, m = map(int, shift_start.split(":"))
    shift_start_dt = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    
    if punch_in_dt <= shift_start_dt:
        return 0
        
    diff = punch_in_dt - shift_start_dt
    minutes = int(diff.total_seconds() // 60)
    return minutes if minutes > 10 else 0

def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    hours = diff_sec / 3600.0
    return float(Decimal(str(hours)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

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
    if value is None: return None
    return float(Decimal(str(value)).quantize(Decimal(f"0.{'0'*decimals}"), rounding=ROUND_HALF_UP))

# Models

class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r'^EMP\d{4,6}$')
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    shift_end: str = Field(default="18:30", pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    joined_on: str 

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

def format_employee(doc):
    doc.pop("_id", None)
    if "created_at" in doc and isinstance(doc["created_at"], datetime):
        doc["created_at"] = to_epoch_ms(doc["created_at"])
    return doc

def format_attendance(doc):
    doc.pop("_id", None)
    if doc.get("punch_in"):
        doc["punch_in"] = to_epoch_ms(doc["punch_in"])
    if doc.get("punch_out"):
        doc["punch_out"] = to_epoch_ms(doc["punch_out"])
    for h in doc.get("history", []):
        if "at" in h: h["at"] = to_epoch_ms(h["at"])
        if "changes" in h:
            for k in ["punch_in", "punch_out"]:
                if k in h["changes"]:
                    if h["changes"][k].get("from"): h["changes"][k]["from"] = to_epoch_ms(h["changes"][k]["from"])
                    if h["changes"][k].get("to"): h["changes"][k]["to"] = to_epoch_ms(h["changes"][k]["to"])
    return doc

# Endpoints provided

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
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    if page < 1: raise HTTPException(422, "page >= 1")
    if page_size < 1 or page_size > 100: raise HTTPException(422, "page_size bounds")
    
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
def punch_in(body: PunchInIn):
    if body.status not in ["PRESENT", "WFH", "ON_DUTY"]:
        raise HTTPException(422, "Invalid status for punch in")

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    if body.punched_at is not None:
        if body.punched_at < 100000000000 or body.punched_at > 4102444800000:
            raise HTTPException(422, "Invalid epoch milliseconds")
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
    else:
        ts = datetime.now(timezone.utc)
        
    # R1: Truncate to whole seconds
    ts = ts.replace(microsecond=0)
    
    # Calculate attendance date
    ts_ist = ts.astimezone(IST)
    att_date = ts_ist.date()
    
    # R1: Overnight shift check
    if emp["shift_end"] <= emp["shift_start"]:
        h_end, m_end = map(int, emp["shift_end"].split(":"))
        if ts_ist.hour < h_end or (ts_ist.hour == h_end and ts_ist.minute < m_end):
            att_date = att_date - timedelta(days=1)
            
    d_str = att_date.isoformat()
    
    doc = {
        "emp_code": body.emp_code,
        "date": d_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts_ist, emp["shift_start"], d_str),
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
    page: int = 1,
    page_size: int = 20,
):
    if page < 1: raise HTTPException(422, "page must be >= 1")
    if page_size < 1 or page_size > 100: raise HTTPException(422, "page_size between 1 and 100")
    
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
        
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from > date_to")
            
    if status:
        if status not in ["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]:
            raise HTTPException(422, "Invalid status")
        q["status"] = status
        
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = list(cursor)
    
    return {
        "items": [format_attendance(d) for d in items], 
        "total": total, 
        "page": page, 
        "page_size": page_size
    }

# Explain Endpoint

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, request: Request):
    valid = ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]
    if endpoint not in valid:
        raise HTTPException(422, "Invalid endpoint")
        
    params = request.query_params
    
    if endpoint == "attendance_list":
        q = {}
        if "emp_code" in params: q["emp_code"] = params["emp_code"]
        if "date_from" in params or "date_to" in params:
            q["date"] = {}
            if "date_from" in params: q["date"]["$gte"] = params["date_from"]
            if "date_to" in params: q["date"]["$lte"] = params["date_to"]
        if "status" in params: q["status"] = params["status"]
        raw = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}}, verbosity="executionStats")
        
    elif endpoint == "employee_monthly":
        emp_code = params.get("emp_code", "EMP0000")
        month = params.get("month", "2000-01")
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}-"}}},
            {"$addFields": {"is_weekday": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}}},
            {"$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, "$is_weekday"]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
            }}
        ]
        raw = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        
    elif endpoint == "department_summary":
        month = params.get("month", "2000-01")
        y, m = map(int, month.split("-"))
        if m == 12: end_date = datetime(y+1, 1, 1).date() - timedelta(days=1)
        else: end_date = datetime(y, m+1, 1).date() - timedelta(days=1)
        
        match_stage = {"joined_on": {"$lte": end_date.isoformat()}}
        if "department" in params: match_stage["department"] = params["department"]
        
        pipeline = [
            {"$match": match_stage},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$emp_code", "$$emp_code"]}, "date": {"$regex": f"^{month}-"}}},
                    {"$addFields": {"is_weekday": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}}}
                ],
                "as": "logs"
            }},
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {"$group": {
                "_id": "$department",
                "headcount_set": {"$addToSet": "$emp_code"},
                "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$eq": ["$logs.is_weekday", True]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
                "work_hours_array": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", "$$REMOVE"]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}
            }},
            {"$project": {
                "_id": 0, "department": "$_id", "headcount": {"$size": "$headcount_set"}, "present_days": 1,
                "avg_work_hours": {"$avg": "$work_hours_array"}, "late_count": 1, "total_late_minutes": 1,
                "leave_count": 1, "on_duty_count": 1
            }},
            {"$sort": {"department": 1}}
        ]
        raw = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        
    elif endpoint == "late_leaderboard":
        month = params.get("month", "2000-01")
        limit = int(params.get("limit", 10))
        pipeline = [
            {"$match": {"date": {"$regex": f"^{month}-"}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"}
        ]
        if "department" in params: pipeline.append({"$match": {"emp.department": params["department"]}})
        pipeline.extend([
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
            {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
            {"$sort": {"rank": 1, "emp_code": 1}}
        ])
        raw = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        
    elif endpoint == "department_trend":
        department = params.get("department", "Unknown")
        from_date = params.get("from", "2000-01-01")
        to_date = params.get("to", "2000-01-31")
        from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
        to_dt = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc)
        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1},
            {"$addFields": {"date_dt": from_dt}},
            {"$densify": {"field": "date_dt", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt + timedelta(days=1)]}}},
            {"$addFields": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}}, "is_working_day": {"$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]}}},
            {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}}], "as": "emps"}},
            {"$addFields": {"headcount": {"$size": "$emps"}}},
            {"$lookup": {"from": "attendance_logs", "let": {"day": "$date", "emp_codes": "$emps.emp_code"}, "pipeline": [{"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$emp_codes"]}]}}}], "as": "logs"}},
            {"$addFields": {
                "present_count": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$cond": [{"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1]}, 0]}}}},
                "late_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$gt": ["$$log.late_minutes", 0]}}}}
            }},
            {"$addFields": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
            {"$setWindowFields": {"sortBy": {"date_dt": 1}, "output": {"moving_avg_7d_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
            {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": "$moving_avg_7d_raw"}},
            {"$sort": {"date": 1}}
        ]
        raw = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        
 # converted bison to json
    return json.loads(json_util.dumps(raw))

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    y, m = map(int, month.split("-"))
    start_date = max(datetime(y, m, 1).date(), datetime.fromisoformat(emp["joined_on"]).date())
    if m == 12:
        end_date = datetime(y+1, 1, 1).date() - timedelta(days=1)
    else:
        end_date = datetime(y, m+1, 1).date() - timedelta(days=1)
        
    working_days = 0
    if start_date <= end_date:
        curr = start_date
        while curr <= end_date:
            if curr.weekday() < 5: working_days += 1
            curr += timedelta(days=1)
            
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$regex": f"^{month}-"}
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
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
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
def department_summary(month: str, department: Optional[str] = None):
    y, m = map(int, month.split("-"))
    if m == 12:
        end_date = datetime(y+1, 1, 1).date() - timedelta(days=1)
    else:
        end_date = datetime(y, m+1, 1).date() - timedelta(days=1)
    end_date_str = end_date.isoformat()
    
    match_stage = {"joined_on": {"$lte": end_date_str}}
    if department:
        match_stage["department"] = department
        
    pipeline = [
        {"$match": match_stage},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp_code": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$emp_code", "$$emp_code"]},
                    "date": {"$regex": f"^{month}-"}
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
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
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
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
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
    
    res = list(db.employees.aggregate(pipeline))
    for d in res:
        d["avg_work_hours"] = round_half_up(d["avg_work_hours"], 2)
        
    return {"month": month, "items": res}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    pipeline = [
        {"$match": {
            "date": {"$regex": f"^{month}-"},
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
    
    res = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": res}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Unknown department")
        
    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc)
    if to_dt < from_dt:
        raise HTTPException(422, "to < from")
    if (to_dt - from_dt).days > 92:
        raise HTTPException(422, "range exceeds 92 days")
        
    pipeline = [
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
                                {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
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
        if body.punched_at < 100000000000 or body.punched_at > 4102444800000:
            raise HTTPException(422, "Invalid epoch milliseconds")
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
    else:
        ts = datetime.now(timezone.utc)
        
    ts = ts.replace(microsecond=0)
    
    record = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "punch_in": {"$lte": ts}
    }, sort=[("punch_in", DESCENDING)])
    
    if not record:
        raise HTTPException(404, "No punch-in found")
        
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
        
    punch_in_dt = record["punch_in"].replace(tzinfo=timezone.utc)
    
    if ts <= punch_in_dt:
        raise HTTPException(422, "punched_at <= punch_in")
        
    if (ts - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(422, "punched_at more than 24 hours after punch_in")
        
    work_h = compute_work_hours(punch_in_dt, ts)
    ot = compute_overtime(ts, emp["shift_end"], record["date"], emp["shift_start"])
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
        if in_ts: in_ts = in_ts.replace(tzinfo=timezone.utc)
        
        if body.punch_in is not None:
            if body.punch_in < 100000000000 or body.punch_in > 4102444800000:
                raise HTTPException(422, "Invalid epoch ms for punch_in")
            in_ts = datetime.fromtimestamp(body.punch_in / 1000, tz=timezone.utc).replace(microsecond=0)
            
        out_ts = record.get("punch_out")
        if out_ts: out_ts = out_ts.replace(tzinfo=timezone.utc)
        
        if body.punch_out is not None:
            if body.punch_out < 100000000000 or body.punch_out > 4102444800000:
                raise HTTPException(422, "Invalid epoch ms for punch_out")
            out_ts = datetime.fromtimestamp(body.punch_out / 1000, tz=timezone.utc).replace(microsecond=0)
            
        if new_status in ["ABSENT", "LEAVE"]:
            if body.punch_in is not None or body.punch_out is not None:
                raise HTTPException(422, "Cannot supply punch times with ABSENT/LEAVE")
            in_ts = None
            out_ts = None
        else:
            if in_ts is None:
                raise HTTPException(422, "Presence status requires punch_in")
                
            ts_ist = in_ts.astimezone(IST)
            att_date = ts_ist.date()
            if emp["shift_end"] <= emp["shift_start"]:
                h_end, m_end = map(int, emp["shift_end"].split(":"))
                if ts_ist.hour < h_end or (ts_ist.hour == h_end and ts_ist.minute < m_end):
                    att_date = att_date - timedelta(days=1)
            
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
        
        if new_status in ["PRESENT", "WFH", "ON_DUTY"]:
            new_late = compute_late_minutes(in_ts.astimezone(IST), emp["shift_start"], date)
            if out_ts is not None:
                new_work = compute_work_hours(in_ts, out_ts)
                new_ot = compute_overtime(out_ts, emp["shift_end"], date, emp["shift_start"])
                new_hd = is_half_day(new_work)
                
        changes = {}
        def check_change(key, old_v, new_v):
            if old_v != new_v:
                changes[key] = {"from": old_v, "to": new_v}
                return new_v
            return old_v
            
        final_doc = {}
        final_doc["status"] = check_change("status", record.get("status"), new_status)
        
        old_in = record.get("punch_in")
        if old_in: old_in = old_in.replace(tzinfo=timezone.utc)
        final_doc["punch_in"] = check_change("punch_in", old_in, in_ts)
        
        old_out = record.get("punch_out")
        if old_out: old_out = old_out.replace(tzinfo=timezone.utc)
        final_doc["punch_out"] = check_change("punch_out", old_out, out_ts)
        
        final_doc["work_hours"] = check_change("work_hours", record.get("work_hours"), new_work)
        final_doc["late_minutes"] = check_change("late_minutes", record.get("late_minutes", 0), new_late)
        final_doc["overtime_minutes"] = check_change("overtime_minutes", record.get("overtime_minutes", 0), new_ot)
        final_doc["half_day"] = check_change("half_day", record.get("half_day", False), new_hd)
        
        if not changes:
            raise HTTPException(422, "Request changes nothing")
            
        history_entry = {
            "at": datetime.now(timezone.utc).replace(microsecond=0),
            "by": body.regularized_by,
            "reason": body.reason,
            "changes": changes
        }
        
        final_doc["history"] = record.get("history", []) + [history_entry]
        
        res = db.attendance_logs.find_one_and_update(
            {"_id": record["_id"], "history": {"$size": len(record.get("history", []))}},
            {"$set": final_doc},
            return_document=ReturnDocument.AFTER
        )
        if res:
            return format_attendance(res)
            
    raise HTTPException(409, "Concurrent modification conflict")
