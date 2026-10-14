# DECISIONS

**Indexes.** Created: employees.emp_code (unique); attendance_logs (emp_code, date) unique, (date desc, emp_code), (status, date desc, emp_code), (emp_code, punch_in desc); employees (department, emp_code), (joined_on), (department, joined_on). Each one serves a query I actually run: listing, the punch-out lookup, analytics date ranges and headcount. I rejected indexes on late_minutes and work_hours: analytics filter by date first, and extra indexes slow every write.

**Punch-in race.** Both requests call insert_one. The unique (emp_code, date) index lets MongoDB accept exactly one; the other gets DuplicateKeyError and I return 409. I do not check first, so there is no gap between the check and the write.

**Leaderboard tie.** I rank with $rank (1, 2, 2, 4) and keep rank <= limit, so everyone tied at the cutoff is included, even if that returns more than limit rows.

**Department summary.** It starts from employees (joined_on <= month end) and uses $lookup for the logs, so people with no logs get an empty list, still count in headcount and add zero.

**100x data.** The trend passes every employee code of the department to $in. At 100x I would store department on each log, or keep a pre-aggregated daily summary collection, to avoid that big list.