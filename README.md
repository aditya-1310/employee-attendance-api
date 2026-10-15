# Employee Attendance & Analytics API

Built with Python 3.11+, FastAPI, PyMongo, and MongoDB 6.0+. The application is
in `app/main.py`. It includes employee creation, attendance punches, manual
corrections, all four analytics endpoints, and query explain output.

## Run locally

Start MongoDB, then run these commands from the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:MONGO_URI = 'mongodb://localhost:27017'
$env:MONGO_DB = 'attendance_db'
uvicorn app.main:app --port 8000
```

For Atlas, set `MONGO_URI` to your database connection string. A local `.env`
also works; environment variables take priority. Keep credentials out of Git.
On Linux/macOS, use `source .venv/bin/activate` and `export` for the variables.

Open `http://localhost:8000/docs` to try the API. Indexes are created at startup.
`GET /health` checks whether MongoDB responds. No sample data is needed to start.

## Run the tests

```powershell
python -m pip install -r requirements-test.txt
$env:TEST_MONGO_URI = 'mongodb://localhost:27017'
python -m pytest -q tests
```

Each test uses a temporary database and removes it afterward. The database
account needs permission to create databases and indexes. Without
`TEST_MONGO_URI`, integration tests are skipped and only the mocked punch-out
tests run.

All 76 tests passed on MongoDB 7.0.16 and 8.0.4. They cover validation, time
rules, rounding, concurrent requests, correction history, pagination, analytics,
and explain plans.

With 100,000 generated logs on MongoDB 8.0.4, startup took under four seconds.
All five explain endpoints showed `IXSCAN` and no `COLLSCAN`. A 92-day trend
query took about 0.8 seconds. These are measurements from the local test setup.

## Notes

All required endpoints are implemented. `REVIEW.md` lists the defects and fixes;
`DECISIONS.md` explains the main choices.

The API accepts and returns timestamps in epoch milliseconds. MongoDB stores
UTC dates; attendance days and shift times use IST. Only manual corrections
append history. Analytics read the stored calculated values.
