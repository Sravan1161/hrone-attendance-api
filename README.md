# Employee Attendance & Analytics API

FastAPI + PyMongo + MongoDB. All code is in `app/main.py`.

## Run
1. `pip install -r requirements.txt`
2. Set `MONGO_URI` and `MONGO_DB` as environment variables (a local `.env` is also read if present).
3. `uvicorn app.main:app --port 8000`

Indexes are created automatically at startup.

## Status
Every endpoint in openapi.yaml is implemented, including `/admin/explain/{endpoint}`.
I tested by hand on the sample data plus a few extra records. I did not run a
100,000-record load test or a parallel-request test.
The trend endpoint accepts at most 92 days (inclusive).

Fixes to the starter code are in REVIEW.md; design decisions are in DECISIONS.md.