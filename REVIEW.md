# Starter review

I reviewed the starter helpers and endpoints, including the unfinished
punch-out code in the working copy. The references below use function names
because the line numbers changed during implementation.

| # | Where | Issue | Example or symptom | What changed |
|---|---|---|---|---|
| 1 | `health` | Reports ready without checking MongoDB. | Stop MongoDB and the endpoint still returns 200. | Added a database ping and a 503 response when it fails. |
| 2 | startup | The database allows duplicate employee codes and attendance records for the same employee and date. | Send identical create or punch-in requests at the same time. | Added unique indexes at startup and handled duplicate-key errors with 409. |
| 3 | `EmployeeIn` | Invalid shift times and equal start/end times are accepted. | Submit `25:00` or the same time for both fields. | Added HH:MM validation and rejected equal shift times. |
| 4 | `EmployeeIn` | Joining dates and email addresses are not validated. | Submit `2026-02-30` or a malformed email. | Added calendar-date validation and an email format check. |
| 5 | `create_employee` | Two requests can pass the duplicate check before either inserts. | Create the same employee in two concurrent requests. | Removed the separate check and let the unique index reject the second insert. |
| 6 | `create_employee` | `created_at` uses local time without a timezone and is returned as a string. | Change the server timezone or inspect the response type. | Changed it to UTC and returned epoch milliseconds. |
| 7 | `list_employees` | `skip = page * page_size` skips the first page. | Request page 1 from a small dataset. | Changed the offset to `(page - 1) * page_size`. |
| 8 | `list_employees` | The total includes employees outside the department filter. | Filter a dataset containing several departments. | Used the same filter for the count and the list. |
| 9 | `list_employees` | Results are not sorted by employee code. | Insert employee codes out of order, then list them. | Added an ascending `emp_code` sort in MongoDB. |
| 10 | both list endpoints | Invalid page numbers and sizes are accepted. | Request page 0 or page size 101. | Required `page >= 1` and a page size between 1 and 100. |
| 11 | `PunchInIn` | Any status string is accepted. | Punch in with LEAVE or an unknown status. | Restricted punch-in to PRESENT, WFH and ON_DUTY. |
| 12 | timestamp inputs | Strings and whole-number floats can pass as integers, and the timestamp range is unchecked. | Send a string, a float, or epoch seconds instead of milliseconds. | Required integer milliseconds within the contract's range. |
| 13 | `punch_in` | An unknown employee causes an error when the code reads their shift. | Punch in with a nonexistent employee code. | Added a 404 check before accessing employee fields. |
| 14 | `punch_in` | The attendance date depends on the server's timezone. | Punch in near IST midnight on a server using UTC. | Used IST to determine the attendance date. |
| 15 | `punch_in` / shift helpers | Overnight shifts get the wrong attendance day and shift end. | Punch in at 02:00 for a 22:00–06:00 shift. | Assigned this punch-in to the previous day and placed shift end on the following morning. |
| 16 | timestamp calculations | Fractional seconds are kept in storage and calculations. | Send a timestamp ending in 900 ms near the grace cutoff. | Truncated timestamps to whole seconds before calculating or storing them. |
| 17 | `compute_late_minutes` | Minutes are rounded down before checking the grace period. | A 09:40:01 punch-in for a 09:30 shift incorrectly returns zero. | Checked whether elapsed seconds exceed 600, then rounded down to minutes. |
| 18 | `compute_work_hours` | Python's `round()` does not apply the required decimal half-up rule. | A duration of 4 hours, 30 minutes and 18 seconds is 4.505 hours and should become 4.51. | Used `Decimal` with half-up rounding to two decimal places. |
| 19 | `compute_overtime` | Overtime is counted before the 30-minute minimum. | Punch out 29 minutes after shift end. | Counted whole minutes and returned zero below 30. |
| 20 | `compute_overtime` | Subtracting datetimes with and without timezone information can fail. | Read a BSON date using PyMongo's default timezone settings. | Treated stored dates without timezone information as UTC before subtracting. |
| 21 | `punch_in` response | The response adds an `id` field and returns timestamps as strings. | Inspect a successful punch-in response. | Removed the internal ID and returned timestamps as integer milliseconds. |
| 22 | `list_attendance` | Every matching record is loaded into Python before sorting and pagination. | Request one page from 100,000 matching records. | Moved sorting and pagination into MongoDB and added supporting indexes. |
| 23 | `list_attendance` | Records with the same date have no defined employee order. | List several employees' records for one day. | Sorted by date descending, then employee code ascending. |
| 24 | `list_attendance` | Invalid dates, reversed ranges and unknown statuses are accepted. | Use `2026-02-30`, a start date after the end date, or an unknown status. | Validated dates, range order and allowed statuses. |
| 25 | attendance responses | Older records can lack default fields, and dates inside history are not converted. Internal IDs are also exposed. | Read an older record or one containing corrections. | Added defaults, removed internal IDs and converted timestamps throughout the response, including history. |
| 26 | unfinished punch-out | The route is missing its leading slash and specifies 201 instead of 200. | The route fails to register; the declared response status also differs from the contract. | Corrected the path and response status. |
| 27 | unfinished punch-out | It searches by the punch-out date instead of finding the latest eligible punch-in. | Punch out after midnight for an overnight shift. | Selected the latest punch-in at or before the requested time, including records already closed. |
| 28 | unfinished punch-out | Employee, duplicate punch-out, duration and half-day checks are missing. | Use an unknown employee, punch out twice, submit equal times or a duration over 24 hours, or finish a short day. | Added the required checks and calculated half-day status from rounded work hours. |
| 29 | unfinished punch-out | An update can overwrite a correction, and two punch-outs can both succeed. | Correct a record between the read and write, or send two punch-outs together. | Made the update conditional on the record still matching what was read. A changed record returns 409. |
| 30 | `EmployeeIn` | Employee-code format and text lengths are unchecked. | Submit `ABC` as the code, an empty name or department, or an overlong field. | Added the format and length checks from the contract. |

## Missing endpoints

I added regularization, the four analytics endpoints and the explain endpoint.
Corrections recalculate the derived fields and record the changes in history.
The analytics calculations run in MongoDB. Explain uses the same queries and
index hints as the corresponding endpoints.

## Follow-up fixes

The first shift validation accepted Unicode digits that the datetime parser
could not read. An employee could pass creation validation but fail at punch-in.
I restricted shift times to ASCII digits (`0`–`9`).

Monthly queries also failed for December 9999 when calculating the start of
the next month. I changed the queries to include the last day of the requested
month instead. Both fixes have regression tests.

## Choices kept

- `load_dotenv()` already preserves values set in the environment, so it needed no change.
- The fixed UTC+05:30 offset is sufficient for the assignment's IST dates and shifts.
- The client supplies `emp_code` because the contract requires it.
- A missing weekday record stays missing; the contract does not require creating an ABSENT record for it.
- Analytics read the stored derived values, including seeded data, as required by the assignment.
- Extra request fields are ignored, including calculated fields. This matches the contract's input rules.
