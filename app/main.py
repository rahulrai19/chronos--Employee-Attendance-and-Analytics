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
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from decimal import Decimal, ROUND_HALF_UP

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
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #

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
