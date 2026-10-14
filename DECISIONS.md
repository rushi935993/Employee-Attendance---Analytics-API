# DECISIONS.md

1. **Indexes.** I use a unique index on `employees.emp_code` for employee identity and a unique compound index on `attendance_logs.emp_code, date` to enforce one attendance record per employee per attendance day under concurrency. The employee department/join-date index supports department headcount filters; a separate join-date index supports headcount without a department filter. Attendance date/employee indexes support list sorting and range queries, while date/late-minute indexing supports the leaderboard. I would avoid a large index containing every analytics field because it would increase write and storage costs without helping all query shapes.

2. **Punch-in race.** Both requests can look up the employee and compute the same attendance date. Each then attempts an insert. MongoDB's unique compound index allows one insert; the other raises `DuplicateKeyError`, which the API translates to 409. Exactly one request receives 201 without relying on a race-prone preliminary attendance check.

3. **Ties.** The leaderboard uses `$rank` over total late minutes, then filters `rank <= limit`. Employees tied at the cutoff rank are all returned, ordered by employee code within the tie.

4. **Headcount.** The department summary starts from employees who joined on or before the month end and looks up attendance logs. Employees with no matching logs remain in the employee stream and contribute to headcount.

5. **100x data.** I would benchmark real query plans, review index selectivity, and consider pre-aggregated daily department metrics if live aggregation latency became too high, while retaining source logs for auditability.
