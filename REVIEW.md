# REVIEW.md

The table records defects found in the starter `app/main.py` and the corresponding fixes in the submitted implementation. The contract is defined by `openapi.yaml`; MongoDB storage types are defined by `DATA_MODEL.md`.

| # | Where | Defect in starter | How to detect it | Fix in this implementation |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Instants were not consistently truncated to whole seconds before calculating lateness. | Check a punch at 09:40:00.900 for a 09:30 shift; it must be treated as 09:40:00. | Normalize to UTC and truncate microseconds before calculating elapsed seconds. |
| 2 | `compute_work_hours` | Punch instants were not truncated, and ordinary `round()` does not guarantee half-up rounding. | Exercise second-level inputs and a duration at a half-centesimal rounding boundary. | Truncate both instants and use `Decimal` with `ROUND_HALF_UP`. |
| 3 | `punch_in` | Stored attendance day did not correctly account for overnight shifts. | For a 22:00–06:00 shift, punch in at 05:45 IST; the attendance day is the previous date. | Convert the instant to IST and assign pre-shift-end overnight punches to the prior day. Store the date as the contract's `YYYY-MM-DD` string. |
| 4 | Employee listing | Pagination offset and filtered total were incorrect. | Request page 2 and/or filter by department; page contents and `total` should reflect the filter. | Use `(page - 1) * page_size` and count the same query used for listing. |
| 5 | Employee creation / database initialization | Concurrent employee creations could insert duplicate `emp_code` values without a unique database constraint. | Send concurrent creates with the same code. | Create a unique `employees.emp_code` index and translate duplicate-key errors to HTTP 409. |
| 6 | Punch-in / database initialization | Concurrent duplicate punches could create multiple records for the same employee and attendance day. | Send simultaneous punch-ins for the same employee/day. | Add a unique compound index on (`emp_code`, `date`) and return HTTP 409 on duplicate-key errors. |
| 7 | `compute_overtime` | Overnight shifts used the same calendar day's shift end. | For a 22:00–06:00 shift, punch out at 06:45 the next day; overtime should be 45 minutes. | Move overnight shift end to the following day and enforce the 30-minute threshold. |
| 8 | Employee input validation | `emp_code` did not enforce the documented format. | Submit `ABC0001` or `EMP123`. | Validate against `^EMP\d{4,6}$`. |
| 9 | Startup initialization | The deprecated `@app.on_event("startup")` hook initialized indexes. | Start the application and inspect the lifecycle hook. | Initialize indexes in FastAPI's lifespan context manager. |

## Confirmed behavior

- Instants are stored as BSON datetimes and exposed as epoch milliseconds. Calendar dates remain `YYYY-MM-DD` strings, as specified in `DATA_MODEL.md`.
- The `health` endpoint checks MongoDB readiness and returns 503 if the ping fails. It intentionally returns a generic client-facing error; diagnostic logging is not currently added.
- The attendance list uses the documented filters, sort, pagination, and filtered total.
