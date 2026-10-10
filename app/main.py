"""
Employee Attendance & Analytics API - STARTER
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

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

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
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

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
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

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
