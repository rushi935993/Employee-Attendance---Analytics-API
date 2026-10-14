"""HROne Employee Attendance & Analytics API (Contract v2.0)."""
import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=2000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
logs = db.attendance_logs
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def ensure_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_joined")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    employees.create_index([("joined_on", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_joined_code")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_employee_date_status")
    logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("late_minutes", DESCENDING)], name="ix_attendance_analytics")
    logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_late_date")
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_employee_punchin")

# Models deliberately reject seconds, floats and strings for timestamp inputs.
def validate_epoch(v):
    if type(v) is not int or not 100_000_000_000 <= v <= 4_102_444_800_000:
        raise ValueError("must be epoch milliseconds in the supported range")
    return v

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def valid_time(cls, v): return validate_epoch(v)

class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    @field_validator("punched_at")
    @classmethod
    def valid_time(cls, v): return validate_epoch(v)

class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_time(cls, v): return validate_epoch(v)
    @model_validator(mode="after")
    def has_edit(self):
        editable = ("status", "punch_in", "punch_out")
        if not any(k in self.model_fields_set for k in editable):
            raise ValueError("at least one attendance field must be supplied")
        for key in editable:
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(f"{key} cannot be null when supplied")
        return self

# Time/serialization helpers
def utc_now(): return datetime.now(UTC).replace(microsecond=0)
def from_ms(ms: int) -> datetime:
    # Integer arithmetic avoids float rounding near second boundaries; truncate, never round.
    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=ms)).replace(microsecond=0)
def to_ms(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)
def as_utc(dt): return dt.replace(tzinfo=UTC) if dt and dt.tzinfo is None else dt
def as_ist(dt): return as_utc(dt).astimezone(IST)
def round2(value): return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
def round4(value): return float(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
def shift_dt(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":")); return datetime.combine(day, time(h, m), IST).astimezone(UTC)
def normalized_timestamp(ms=None):
    return from_ms(ms) if ms is not None else utc_now()
def attendance_day(punch_in: datetime, emp):
    local = as_ist(punch_in); d = local.date()
    if emp["shift_end"] <= emp["shift_start"]:
        end_h, end_m = map(int, emp["shift_end"].split(":"))
        if (local.hour, local.minute) < (end_h, end_m): d -= timedelta(days=1)
    return d.isoformat()
def late_minutes(punch_in, emp):
    local = as_ist(punch_in); day = date.fromisoformat(attendance_day(punch_in, emp))
    start = shift_dt(day, emp["shift_start"])
    elapsed = int((as_utc(punch_in) - start).total_seconds())
    return elapsed // 60 if elapsed > 600 else 0
def overtime_minutes(punch_out, emp, daystr):
    day = date.fromisoformat(daystr)
    overnight = emp["shift_end"] <= emp["shift_start"]
    end = shift_dt(day + (timedelta(days=1) if overnight else timedelta()), emp["shift_end"])
    mins = int((as_utc(punch_out) - end).total_seconds()) // 60
    return mins if mins >= 30 else 0
def compute_derived(status, pin, pout, emp, daystr):
    if status not in PRESENT: return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = late_minutes(pin, emp) if pin else 0
    if not pout or not pin: return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    seconds = int((as_utc(pout) - as_utc(pin)).total_seconds())
    hours = round2(Decimal(seconds) / Decimal(3600))
    return {"work_hours": hours, "late_minutes": late, "overtime_minutes": overtime_minutes(pout, emp, daystr), "half_day": hours < 4.5}
def clean_doc(doc, employee=False):
    if not doc: return doc
    doc = dict(doc); doc.pop("_id", None)
    if employee:
        if isinstance(doc.get("created_at"), datetime): doc["created_at"] = to_ms(doc["created_at"])
        return doc
    for f in ("punch_in", "punch_out"):
        if isinstance(doc.get(f), datetime): doc[f] = to_ms(doc[f])
    doc.setdefault("history", []); doc.setdefault("half_day", False); doc.setdefault("late_minutes", 0); doc.setdefault("overtime_minutes", 0)
    for h in doc["history"]:
        if isinstance(h.get("at"), datetime): h["at"] = to_ms(h["at"])
        for f in ("punch_in", "punch_out"):
            if f in h.get("changes", {}):
                for side in ("from", "to"):
                    if isinstance(h["changes"][f].get(side), datetime): h["changes"][f][side] = to_ms(h["changes"][f][side])
    return doc
def month_bounds(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month): raise HTTPException(422, "month must be YYYY-MM")
    y,m=map(int,month.split("-")); start=date(y,m,1); end=date(y,m,calendar.monthrange(y,m)[1])
    return start.isoformat(), end.isoformat(), start, end
def weekdays(start: date, end: date):
    if end < start: return 0
    n=(end-start).days+1
    full,rem=divmod(n,7); count=full*5
    for i in range(rem):
        if (start.weekday()+i)%7 < 5: count+=1
    return count
def month_pipeline_match(month):
    a,b,_,_=month_bounds(month); return {"date":{"$gte":a,"$lte":b}}
def aggregate_one(coll, pipeline): return list(coll.aggregate(pipeline, allowDiskUse=True))

def explain_aggregate(coll, pipeline):
    return db.command({"explain":{"aggregate":coll.name,"pipeline":pipeline,"cursor":{}},"verbosity":"executionStats"})

@app.get("/health")
def health():
    try: client.admin.command("ping"); return {"status":"ok"}
    except PyMongoError: raise HTTPException(503, "MongoDB unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc=body.model_dump(); doc["joined_on"]=doc["joined_on"].isoformat(); doc["created_at"]=utc_now()
    try: employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409, "emp_code already exists")
    return clean_doc(doc, True)

@app.get("/employees")
def list_employees(department: Optional[str]=None, page: int=Query(1,ge=1), page_size: int=Query(20,ge=1,le=100)):
    q={"department":department} if department else {}
    total=employees.count_documents(q)
    items=[clean_doc(x,True) for x in employees.find(q,{"_id":0}).sort("emp_code",ASCENDING).skip((page-1)*page_size).limit(page_size)]
    return {"items":items,"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-in",status_code=201)
def punch_in(body:PunchInIn):
    emp=employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    ts=normalized_timestamp(body.punched_at); day=attendance_day(ts,emp)
    doc={"emp_code":body.emp_code,"date":day,"status":body.status,"punch_in":ts,"punch_out":None,"work_hours":None,"late_minutes":late_minutes(ts,emp),"overtime_minutes":0,"half_day":False,"history":[]}
    try: logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"already punched in for this date")
    return clean_doc(doc)

@app.post("/attendance/punch-out")
def punch_out(body:PunchOutIn):
    emp=employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    ts=normalized_timestamp(body.punched_at)
    rec=logs.find_one({"emp_code":body.emp_code,"punch_in":{"$ne":None,"$lte":ts}},sort=[("punch_in",DESCENDING)])
    if not rec: raise HTTPException(404,"no punch-in found")
    if rec.get("punch_out") is not None: raise HTTPException(409,"already punched out")
    pin=as_utc(rec["punch_in"])
    if ts <= pin or (ts-pin).total_seconds()>86400: raise HTTPException(422,"punch-out must be after punch-in and within 24 hours")
    der=compute_derived(rec["status"],pin,ts,emp,rec["date"])
    updated=logs.find_one_and_update({"_id":rec["_id"],"punch_out":None},{"$set":{"punch_out":ts,**der}},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,"already punched out")
    return clean_doc(updated)

@app.get("/attendance")
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[date]=None,date_to:Optional[date]=None,status:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    if date_from and date_to and date_from>date_to: raise HTTPException(422,"date_from must be on or before date_to")
    q={}
    if emp_code: q["emp_code"]=emp_code
    if date_from or date_to:
        q["date"]={}
        if date_from:q["date"]["$gte"]=date_from.isoformat()
        if date_to:q["date"]["$lte"]=date_to.isoformat()
    if status:
        if status not in STATUSES: raise HTTPException(422,"invalid status")
        q["status"]=status
    total=logs.count_documents(q)
    items=[clean_doc(x) for x in logs.find(q,{"_id":0}).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip((page-1)*page_size).limit(page_size)]
    return {"items":items,"total":total,"page":page,"page_size":page_size}

@app.patch("/attendance/{emp_code}/{date_str}")
def regularize(emp_code:str,date_str:str,body:RegularizeIn):
    date = date_str
    try: day=__import__("datetime").date.fromisoformat(date_str)
    except Exception: raise HTTPException(422,"date must be YYYY-MM-DD")
    if day.isoformat()!=date: raise HTTPException(422,"date must be YYYY-MM-DD")
    emp=employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"unknown employee")
    old=logs.find_one({"emp_code":emp_code,"date":date})
    if not old: raise HTTPException(404,"attendance record not found")
    changes_in=body.model_dump(exclude_unset=True); changes_in.pop("reason",None); changes_in.pop("regularized_by",None)
    status=changes_in.get("status",old["status"])
    pin=from_ms(changes_in["punch_in"]) if "punch_in" in changes_in else as_utc(old.get("punch_in")) if old.get("punch_in") else None
    pout=from_ms(changes_in["punch_out"]) if "punch_out" in changes_in else as_utc(old.get("punch_out")) if old.get("punch_out") else None
    if status in {"ABSENT","LEAVE"}:
        if ("punch_in" in changes_in and changes_in["punch_in"] is not None) or ("punch_out" in changes_in and changes_in["punch_out"] is not None): raise HTTPException(422,"ABSENT and LEAVE cannot have punch times")
        pin=pout=None
    else:
        if pin is None: raise HTTPException(422,"presence status requires punch_in")
        if attendance_day(pin,emp)!=date: raise HTTPException(422,"punch_in must belong to the attendance date")
        if pout is not None and (pout<=pin or (pout-pin).total_seconds()>86400): raise HTTPException(422,"invalid punch_out")
    der=compute_derived(status,pin,pout,emp,date)
    new_fields={"status":status,"punch_in":pin,"punch_out":pout,**der}
    actual={}
    for k,v in new_fields.items():
        ov=old.get(k,False if k=="half_day" else 0 if k in ("late_minutes","overtime_minutes") else None)
        if k in ("punch_in","punch_out"): ov=as_utc(ov) if ov else None
        if ov != v: actual[k]={"from":ov,"to":v}
    if not actual: raise HTTPException(422,"request does not change the record")
    entry={"at":utc_now(),"by":body.regularized_by,"reason":body.reason,"changes":actual}
    # Compare-and-swap on current history length and values avoids lost audit entries.
    filt={"_id":old["_id"],"status":old["status"],"punch_in":old.get("punch_in"),"punch_out":old.get("punch_out")}
    if "history" in old: filt["history"]=old["history"]
    else: filt["$or"]=[{"history":{"$exists":False}},{"history":[]}]
    upd=logs.find_one_and_update(filt,{"$set":new_fields,"$push":{"history":entry}},return_document=ReturnDocument.AFTER)
    if not upd: raise HTTPException(409,"record changed concurrently; retry")
    return clean_doc(upd)

# Explicit alias route implementation (Python cannot use `from` as a parameter name).
@app.get("/analytics/departments/{department}/trend")
def department_trend_contract(department:str, from_:str=Query(...,alias="from"), to_:str=Query(...,alias="to")):
    try: start=date.fromisoformat(from_); end=date.fromisoformat(to_)
    except ValueError: raise HTTPException(422,"from and to must be YYYY-MM-DD")
    if end<start: raise HTTPException(422,"to must be on or after from")
    if (end-start).days+1>92: raise HTTPException(422,"range may not exceed 92 days")
    if not employees.find_one({"department":department}): raise HTTPException(404,"unknown department")
    # Date densification, daily department totals, headcount lookup, and moving average stay in MongoDB.
    start_dt=datetime.combine(start,time.min,IST).astimezone(UTC); end_dt=datetime.combine(end,time.min,IST).astimezone(UTC)
    pipeline = [
        {"$documents": [{"day": start_dt}]},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt + timedelta(days=1)]}}},
        {"$set": {"date_str": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date_str"}, "pipeline": [
            {"$match": {"department": department}}, {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}}, {"$count": "n"}
        ], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$date_str"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
            {"$unwind": "$e"}, {"$match": {"$expr": {"$eq": ["$e.department", department]}}},
            {"$group": {"_id": None,
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENT)]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}
            }}
        ], "as": "daily"}},
        {"$set": {
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]},
            "is_working_day": {"$not": [{"$in": [{"$dayOfWeek": {"date": "$day", "timezone": "Asia/Kolkata"}}, [1, 7]]}]}
        }},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, _round_half_up_expr({"$divide": ["$present_count", "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$date_str", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [{"$eq": [{"$type": "$moving_avg_7d"}, "missing"]}, None, _round_half_up_expr("$moving_avg_7d", 4)]}}},
        {"$sort": {"date": 1}}
    ]
    rows=list(db.aggregate(pipeline, allowDiskUse=True))
    return {"department":department,"items":rows}

# Aggregation helpers and analytics endpoints (all analytics calculations run in MongoDB).
def _month_dates(month):
    a, b, start, end = month_bounds(month)
    return a, b, start, end

def _date_expr(field):
    return {"$dateFromString": {"dateString": field, "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}}

def _weekday_expr(date_field):
    return {"$in": [{"$dayOfWeek": {"date": _date_expr(date_field), "timezone": "Asia/Kolkata"}}, [2, 3, 4, 5, 6]]}

def _round_half_up_expr(expr, places=2):
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, scale]}, 0.5]}}, scale]}

def _log_metrics_expr(logs_field="$month_logs"):
    present = {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]}
    return {
        "present_days": {"$sum": {"$map": {"input": logs_field, "as": "l", "in": {"$cond": [{"$and": [present, _weekday_expr("$$l.date")]}, {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1]}, 0]}}}},
        "leave_days": {"$size": {"$filter": {"input": logs_field, "as": "l", "cond": {"$eq": ["$$l.status", "LEAVE"]}}}},
        "late_count": {"$size": {"$filter": {"input": logs_field, "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}},
        "total_late_minutes": {"$sum": {"$map": {"input": logs_field, "as": "l", "in": {"$ifNull": ["$$l.late_minutes", 0]}}}},
        "total_overtime_minutes": {"$sum": {"$map": {"input": logs_field, "as": "l", "in": {"$ifNull": ["$$l.overtime_minutes", 0]}}}},
    }

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    a, b, start, end = _month_dates(month)
    pipeline = [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"date": {"$gte": a, "$lte": b}}}, {"$match": {"$expr": {"$eq": ["$emp_code", "$$code"]}}}], "as": "month_logs"}},
        {"$addFields": {"effective_start": {"$max": ["$joined_on", a]}, "day_count": {"$dateDiff": {"startDate": _date_expr({"$max": ["$joined_on", a]}), "endDate": _date_expr(b), "unit": "day"}}}},
        {"$addFields": {"calendar_days": {"$map": {"input": {"$range": [0, {"$max": [0, {"$add": ["$day_count", 1]}]}]}, "as": "n", "in": {"$dateToString": {"date": {"$dateAdd": {"startDate": _date_expr("$effective_start"), "unit": "day", "amount": "$$n"}}, "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}}}}}},
        {"$addFields": {"working_days": {"$size": {"$filter": {"input": "$calendar_days", "as": "d", "cond": _weekday_expr("$$d")}}}}},
        {"$addFields": _log_metrics_expr()},
        {"$addFields": {"attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]}, _round_half_up_expr({"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]}, 4), None]}}},
        {"$project": {"_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": 1, "present_days": _round_half_up_expr("$present_days", 2), "leave_days": 1, "late_count": 1, "total_late_minutes": 1, "total_overtime_minutes": 1, "attendance_pct": 1}},
    ]
    rows = aggregate_one(employees, pipeline)
    if not rows: raise HTTPException(404, "unknown employee")
    return rows[0]

@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    a, b, _, _ = _month_dates(month)
    match = {"joined_on": {"$lte": b}}
    if department is not None: match["department"] = department
    pipeline = [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [{"$match": {"date": {"$gte": a, "$lte": b}}}, {"$match": {"$expr": {"$eq": ["$emp_code", "$$code"]}}}], "as": "month_logs"}},
        {"$addFields": _log_metrics_expr()},
        {"$addFields": {
            "avg_hours_sum": {"$sum": {"$map": {
                "input": {"$filter": {"input": "$month_logs", "as": "l", "cond": {"$and": [
                    {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$$l.work_hours", None]}
                ]}}}, "as": "l", "in": "$$l.work_hours"
            } }},
            "avg_hours_n": {"$size": {"$filter": {"input": "$month_logs", "as": "l", "cond": {"$and": [
                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$$l.work_hours", None]}
            ]}}}},
            "on_duty_count": {"$size": {"$filter": {"input": "$month_logs", "as": "l", "cond": {"$eq": ["$$l.status", "ON_DUTY"]}}}}
        }},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$present_days"}, "late_count": {"$sum": "$late_count"}, "total_late_minutes": {"$sum": "$total_late_minutes"}, "leave_count": {"$sum": "$leave_days"}, "on_duty_count": {"$sum": "$on_duty_count"}, "hours_sum": {"$sum": "$avg_hours_sum"}, "hours_n": {"$sum": "$avg_hours_n"}}},
        {"$sort": {"_id": 1}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": _round_half_up_expr("$present_days", 2), "avg_work_hours": {"$cond": [{"$gt": ["$hours_n", 0]}, _round_half_up_expr({"$divide": ["$hours_sum", "$hours_n"]}, 2), None]}, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}},
    ]
    return {"month": month, "items": aggregate_one(employees, pipeline)}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    a, b, _, _ = _month_dates(month)
    pipeline = [
        {"$match": {"date": {"$gte": a, "$lte": b}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return {"month": month, "items": aggregate_one(logs, pipeline)}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None, limit: int = Query(10, ge=1, le=50), date_from: Optional[date] = None, date_to: Optional[date] = None, status: Optional[str] = None, from_date: Optional[date] = Query(None, alias="from"), to_date: Optional[date] = Query(None, alias="to"), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if endpoint not in {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}:
        raise HTTPException(422, "unknown endpoint")
    if endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from.isoformat()
            if date_to: q["date"]["$lte"] = date_to.isoformat()
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be on or before date_to")
        if status:
            if status not in STATUSES: raise HTTPException(422, "invalid status")
            q["status"] = status
        exp = db.command({"explain": {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size}, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if not month: raise HTTPException(422, "month is required")
    _month_dates(month)
    if endpoint == "employee_monthly":
        if not emp_code: raise HTTPException(422, "emp_code is required")
        if not employees.find_one({"emp_code": emp_code}, {"_id": 1}): raise HTTPException(404, "unknown employee")
        a, b, _, _ = _month_dates(month)
        pipeline = [
            {"$match": {"emp_code": emp_code}},
            {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
                {"$match": {"date": {"$gte": a, "$lte": b}}},
                {"$match": {"$expr": {"$eq": ["$emp_code", "$$code"]}}
            }], "as": "month_logs"}},
            {"$project": {"_id": 0, "emp_code": 1, "month_logs": 1}}
        ]
        exp = explain_aggregate(employees, pipeline)
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}
    if endpoint == "department_summary":
        a, b, _, end = _month_dates(month)
        q = {"joined_on": {"$lte": end.isoformat()}}
        if department is not None: q["department"] = department
        pipeline = [
            {"$match": q},
            {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
                {"$match": {"date": {"$gte": a, "$lte": b}}},
                {"$match": {"$expr": {"$eq": ["$emp_code", "$$code"]}}
            }], "as": "month_logs"}},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}
        ]
        exp = explain_aggregate(employees, pipeline)
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}
    if endpoint == "late_leaderboard":
        a, b, _, _ = _month_dates(month)
        pipeline = [
            {"$match": {"date": {"$gte": a, "$lte": b}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"}
        ]
        if department is not None: pipeline.append({"$match": {"employee.department": department}})
        pipeline.extend([
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}}
        ])
        exp = explain_aggregate(logs, pipeline)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if endpoint == "department_trend":
        if not department or from_date is None or to_date is None: raise HTTPException(422, "department, from and to are required")
        if to_date < from_date or (to_date-from_date).days+1 > 92: raise HTTPException(422, "invalid date range")
        exp = explain_aggregate(logs, [
            {"$match": {"date": {"$gte": from_date.isoformat(), "$lte": to_date.isoformat()}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department}}
        ])
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    raise HTTPException(422, "invalid endpoint")
