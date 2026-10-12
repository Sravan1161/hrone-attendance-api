
# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | compute_late_minutes, line 37 | Uses `minutes > 10` on whole minutes, so 09:40:01 (10 min 1 sec) is treated as on time. Late must be "strictly more than 10:00" in seconds. | Punch in 09:40:01 on a 09:30 shift. Expected 10, got 0. | Compare in seconds (`> 600`), then take floor of minutes. |
| 2 | compute_late_minutes, line 35 | Builds shift start from the punch's own date and time zone, not IST. Overnight shifts are not handled (R1). | Punch in just after midnight on a 22:00-06:00 shift gives a wrong number. | Convert punch_in to IST first, and use the previous day's shift start for overnight shifts. |
| 3 | compute_work_hours, line 41 | Uses Python `round()`, which is not half-up (R4). | 8.125 hours gives 8.12 instead of 8.13. | Use Decimal with ROUND_HALF_UP. |
| 4 | compute_overtime, line 47 | No 30-minute minimum (R3). Overnight shift end should be the next day. Mixing naive and aware datetimes can crash. | Punch out 10 min after shift end gives 10, should be 0. | Return 0 if under 30, add a day for overnight shifts, use timezone-aware datetimes. |
| 5 | EmployeeIn / PunchInIn, lines 53-66 | No validation: any status, any punched_at (even seconds), no format checks on emp_code, email, shift times, joined_on. shift_start can equal shift_end. | POST status "FOO" or punched_at 1783312500 (seconds). Should be 422, gets accepted. | Use Pydantic patterns, an enum for status, range checks on punched_at, and a check that shift_start != shift_end. |
| 6 | create_employee, lines 79-83 | Check-then-insert race. No unique index, so two requests at once can both insert. | Send 10 identical POST /employees at once. More than one returns 201. | Unique index on emp_code at startup, catch DuplicateKeyError and return 409. |
| 7 | create_employee, line 82 | `datetime.now()` is local naive time, and it is returned as a datetime, not epoch milliseconds. | created_at in the response is a string, not an integer. | Use UTC time, store it, and return epoch ms. |
| 8 | list_employees, line 93 | `skip = page * page_size` skips the whole first page. | GET /employees?page=1 is missing EMP0001. | Use `(page - 1) * page_size`. |
| 9 | list_employees, line 94 | `count_documents({})` ignores the department filter. | Filter by one department: items shows 3 but total shows 6. | Count with the same filter `q`. |
| 10 | list_employees, line 95 | No sort, so order and paging are not guaranteed. | Create employees out of order and list them. | Add `.sort("emp_code", 1)`. |
| 11 | list_employees, line 89 | No limits on page and page_size. page=0 gives a negative skip, a huge page_size is allowed. | GET /employees?page=0 gives a 500 error. | Validate page >= 1 and 1 <= page_size <= 100 (422 otherwise). |
| 12 | punch_in, line 101 and 113 | Unknown employee gives `emp = None`, then `emp["shift_start"]` crashes with a 500. | POST punch-in for EMP9999. Expected 404. | Return 404 if the employee is not found. |
| 13 | punch_in, lines 102-103 | Uses local naive time, not IST. The date is wrong near midnight, overnight rule is missing, seconds are not truncated (R1). | Punch in at 23:50 IST gets the wrong date when the server is not in IST. | Convert to IST, apply the overnight rule, truncate to whole seconds, store in UTC. |
| 14 | punch_in, lines 104-118 | Same race as #6. No unique index on (emp_code, date). | 10 identical punch-ins at once give more than one 201. | Unique index on (emp_code, date), catch DuplicateKeyError and return 409. |
| 15 | punch_in and list_attendance, lines 119 and 149 | Returns `id` made from `_id`. The contract says `_id` and any id never appear. Times are returned as datetimes, not epoch ms. | The response has an "id" field and ISO date strings. | Remove `id`, convert times to epoch ms in one helper. |
| 16 | list_attendance, lines 144-147 | Loads every document into memory, sorts and pages in Python. Sort has no emp_code tie-break and no index. Slow at 100,000 records. | Call on a big collection is slow. | Sort, skip and limit in MongoDB (date desc, emp_code asc) with an index, and use count_documents. |
| 17 | list_attendance, lines 136-150 | Old records missing half_day and history are returned without defaults. date_from later than date_to is not rejected. | Sample record 9 has no history key. date_from > date_to returns 200. | Default half_day to False and history to []. Return 422 for a bad date range. |
| 18 | /health, line 74 | Always returns ok and never checks MongoDB. | Stop MongoDB and call /health. It still says ok. | Ping MongoDB, return 503 if it fails. |
| 19 | Startup, lines 22-23 | No indexes are created, and MongoClient is not `tz_aware`, so datetimes come back naive. | explain shows COLLSCAN. Datetimes have no tzinfo. | Create indexes at startup (idempotent), and use `tz_aware=True`. |

## Things I looked at and decided were fine

- `load_dotenv()` does not override real environment variables by default, so real variables win, as the contract requires.
- The `IST` constant (UTC+05:30) is correct.
- `int(total_seconds() / 60)` is fine for positive values (it floors them).
- `status_code=201` on the two POST endpoints is correct.