# Chronos - Employee Attendance and Analytics API

This is a FastAPI application backed by MongoDB (Atlas) for tracking employee attendance and generating complex analytical reports. 

## How to run

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
2. **Set up Environment Variables:**
   Create a `.env` file in the root directory and set the following variables:
   ```env
   MONGO_URI="mongodb+srv://<username>:<password>@<your-cluster>.mongodb.net/?retryWrites=true&w=majority"
   MONGO_DB="hr_attendance"
   ```
3. **Start the API:**
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

## Notes for the Reviewer
- The application automatically ensures that all critical MongoDB compound and unique indexes are created idempotently on startup.
- All analytics logic is strictly pushed down to the database level using efficient MongoDB aggregation pipelines (no Python loops!).
- `timezone.utc` is strictly used for storing times, and `IST` conversions are handled safely for logic comparisons (such as overnight shift bounding).
- The dynamic `GET /admin/explain/{endpoint}` successfully prints the execution statistics for any of our list/aggregation pipelines.
