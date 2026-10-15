# Local API tests

Install the test dependencies:

```powershell
.\.venv\Scripts\python -m pip install -r requirements-test.txt
```

Run the complete suite against MongoDB 6.0 or later:

```powershell
$env:TEST_MONGO_URI = 'mongodb://127.0.0.1:27017'
.\.venv\Scripts\python -m pytest -q tests
```

Each test creates an isolated database and deletes that database afterward.
Tests cover validation, pagination, simultaneous creates and punches,
correction history, analytics, ties, missing dates, joining dates, and explain
plans. The punch-out tests are included in the same run.

The integration tests need a real MongoDB server. Without `TEST_MONGO_URI`,
only the mocked punch-out tests run. Those cannot verify aggregation or
simultaneous database writes.

This workspace also has a downloaded MongoDB server in `.test-mongo`, which is
ignored by Git. To run the tests with it:

```powershell
.\.venv\Scripts\python tests/run_local_mongo.py
```

To use the separately downloaded MongoDB 7.0 archive in this workspace:

```powershell
.\.venv\Scripts\python tests/run_local_mongo.py --mongo-version 7
```

The helper starts that server on localhost port 27028, runs all tests, and stops
its server afterward. MongoDB 7 uses port 27029 and a separate data directory.
The helper verifies the selected server major version. It does not download
MongoDB or require Docker. Do not run it while another process is already using
the selected port.

All 76 tests passed on MongoDB 7.0.16 and 8.0.4. This includes the fixes for
Unicode shift digits and December 9999 across monthly analytics and explain.
