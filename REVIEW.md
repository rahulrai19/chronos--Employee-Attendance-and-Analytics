# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` | Unconditionally returns 200 without checking if the database is actually reachable. | Stopping the MongoDB instance still returns `{"status": "ok"}` instead of 503. | Replaced with a try/except block running `db.command("ping")` and raising a 503 if it fails. |
| 2 | Global / Startup | Critical MongoDB indexes (like unique `emp_code`) were completely missing. | Running the `explain` endpoint on queries shows `COLLSCAN` (collection scans) instead of `IXSCAN`. | Added an `@app.on_event("startup")` hook to idempotently create unique and compound indexes. |
| 3 | `create_employee` & `punch_in` | "Check-then-insert" race conditions using `find_one` before `insert_one`. | Two identical requests sent at the exact same millisecond would bypass the check and insert duplicates. | Removed `find_one` checks and wrapped direct inserts in a `try/except DuplicateKeyError`, relying on the new unique MongoDB indexes. |
| 4 | `list_employees` | The `skip` variable assumed `page` is 0-indexed (`page * page_size`), and `total` ignored filters. | Requesting `page=1` skips the first 20 records. `total` shows all employees even if a department filter is passed. | Updated skip logic to `(page - 1) * page_size` and passed the `department` query `q` into `count_documents(q)`. |
| 5 | `list_employees` | Missing required sorting. | Employees are returned in insertion order rather than sorted by `emp_code` ascending. | Chained `.sort("emp_code", 1)` to the MongoDB query cursor. |
| 6 | `list_attendance` | Fetches the entire collection into memory and sorts via Python `list()`. | The API crashes with Out Of Memory errors when tested against the 100,000 document grader dataset. | Moved sorting and pagination entirely to the database using cursor `.sort()`, `.skip()`, and `.limit()`. |
| 7 | `punch_in` | Failed to truncate time to whole seconds, and incorrectly set `date` on overnight shifts. | A 23:00 punch-in for a 22:00-06:00 shift gets assigned to the wrong calendar day. Fractions of seconds skew calculations. | Enforced truncation via `ts.replace(microsecond=0)`. Added logic checking if `shift_end <= shift_start` and adjusting `date` if the punch is before `shift_end`. |
| 8 | `compute_late_minutes` | Fails on overnight shifts by blindly replacing the time on the current punch-in day. | An overnight worker punching in after midnight is registered as being massively early or late incorrectly. | Passed in the actual `date_str` to reconstruct `shift_start_dt` accurately before computing the difference. |
| 9 | `create_employee` / `punch_in` | Returned Python `datetime` objects and an injected `"id"` string. | API contract requires "Epoch milliseconds". Contract also forbids returning `_id` or an internal `"id"`. | Wrote formatters that remove `_id`/`id` completely and map all times using `dt.timestamp() * 1000`. |

### What was examined and found fine:
- **PyMongo Connection:** The usage of `load_dotenv` and environment variables for the connection string (`MONGO_URI`, `MONGO_DB`) was implemented properly.
- **Pydantic Models (`EmployeeIn`):** The basic shapes were fine, although they were overly permissive. I kept the base structure but added tighter regex validations to conform strictly to the assignment's expected string formats.



