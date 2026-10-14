# Decisions

1. **Indexes.** A unique index on `employees.emp_code` prevents duplicate employee codes. A unique (`emp_code`, `date`) index makes punch-in race-safe. Employee department/join-date indexes support filtered lists and department analytics. Attendance indexes support employee/date lookups, date-sorted lists, status filters, and late-month scans. The extra reversed employee/join-date index may be unnecessary; compare `executionStats` before keeping it. Indexes trade storage and write cost for read speed.

2. **Punch-in race.** Both requests can pass application checks, but the unique compound index allows only one insert. The losing insert raises `DuplicateKeyError`, which becomes HTTP 409.

3. **Ties.** The late leaderboard uses `$rank` over total late minutes and includes every employee whose rank is at most `limit`; ties at the cutoff are included. Results with the same total are ordered by employee code for stable output.

4. **Headcount.** Monthly department summaries start from employees and look up attendance, so employees with no logs still contribute to headcount. Employees joining after the period are filtered out using `joined_on`.

5. **At 100x data.** Use the explain endpoint and production-like load measurements to tune indexes and aggregation pipelines; consider pre-aggregating monthly analytics if measured query cost warrants it.
