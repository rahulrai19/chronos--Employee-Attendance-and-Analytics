# REVIEW

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `health` | Always returns 200 without touching MongoDB. | Stop MongoDB: `/health` still returns `{"status":"ok"}` instead of 503. | `db.command("ping")` in try/except; raise 503 on failure. |
| 2 | Startup | The indexes the endpoints need did not exist. | `/admin/explain` shows COLLSCAN; duplicates are possible. | Lifespan handler creates all indexes idempotently: unique `emp_code`, `department`, `joined_on` on employees; unique `(emp_code, date)`, `(date desc, emp_code)`, `(emp_code, punch_in desc)` on attendance_logs. |
| 3 | `create_employee`, `punch_in` | Check-then-insert (`find_one` then `insert_one`). | Two simultaneous identical requests both pass the check: two 201s and a duplicate. | Removed the pre-check; rely on the unique indexes and catch `DuplicateKeyError` -> 409. |
| 4 | `list_employees` | `skip = page * page_size` (0-indexed) and `total` ignored the filter. | 25 employees, `page=1`: first 20 skipped. `department=X` still reports the full total. | `skip = (page-1)*page_size`; `count_documents(q)` with the same filter. |
| 5 | `list_employees` | No sort. | Results come back in insertion order, not by `emp_code`. | `.sort("emp_code", 1)`. |
| 6 | `list_attendance` | Sorted and sliced in Python after fetching documents. | Latency and memory grow with collection size; explain shows COLLSCAN. | Sort, skip and limit in the cursor; backed by the `(date desc, emp_code)` index. |
| 7 | `punch_in` | Instants not truncated to whole seconds (R1). | Punch-in at `…:40:00.500` on shift 09:30 is treated as 600.5 s after start and counted late, instead of 0. | `ts.replace(microsecond=0)` before any calculation; stored and returned truncated. |
| 8 | `punch_in` (date) | Attendance date ignored the overnight rule. | Shift 22:00-06:00, punch-in 02:00 IST on the 15th is dated the 15th; R1 says the 14th. | If `shift_end <= shift_start` and the punch-in is earlier than `shift_end`, use the previous day (shared helper). |
| 9 | `compute_late_minutes` (date base) | Shift start was built on the punch-in's own calendar day. | Same overnight shift, punch-in 00:30 on the 15th: shift start becomes the 15th 22:00, so late = 0. It should be 150. | Build shift start from the record's attendance date. |
| 10 | `compute_late_minutes` (grace) | Floors to minutes first, then tests `minutes > 10`. | Shift 09:30, punch-in 09:40:01 returns 0; R2 says 10. | Compare seconds (`> 600`), then floor to minutes. |
| 11 | Datetime handling / `to_epoch_ms` | PyMongo returns naive datetimes, which `.timestamp()` reads as local time; float math can be off by 1 ms. | On a non-UTC server, punch times from `GET /attendance` differ from the punch-in response by the UTC offset. | `MongoClient(tz_aware=True)`; integer arithmetic for epoch ms; removed per-call `tzinfo` patches. |
| 12 | `PunchIn` (`punched_at`) | `Optional[int]` coerces strings and floats. | `"punched_at": "1700000000000"` or `1700000000000.0` returns 201; contract says 422. | `StrictInt`; one shared `parse_epoch_ms` with range check. |
| 13 | `EmployeeIn.joined_on` | Plain `str`, never validated. | `"joined_on": "hello"` returns 201; later date parsing and string comparisons break. | Pattern plus `date.fromisoformat` validator -> 422. |
| 14 | `format_employee`, `format_attendance` | Returned internal fields and non-contract types; mutated the input; missing `history` / `half_day` were not defaulted. | `_id` or `id` in responses; a seeded record without `history` returns no such field. | Return a new dict with epoch ms, no `_id`/`id`, `history: []` and `half_day: false` defaults. |
| 15 | Validation handler | `exc.errors()` can contain non-serializable objects. | A validator that raises `ValueError` returns 500 instead of 422. | `jsonable_encoder(exc.errors())`. |

## Examined and judged fine
- `compute_overtime`: floor to minutes, threshold >= 30, overnight shift end handled.
- `is_half_day`: called with the already-rounded `work_hours` (R5).
- `load_dotenv` with `MONGO_URI` / `MONGO_DB`: real environment variables keep priority.
- `emp_code`, `email` and shift-time patterns in `EmployeeIn` (only `joined_on` was loose).
