# Design decisions

1. **Indexes.** Unique indexes on `emp_code` and `(emp_code, date)` prevent duplicates. The second also supports monthly log lookups. `(date descending, emp_code)` supports attendance lists; putting status first covers status filters. `(emp_code, punch_in descending)` finds the latest punch-in. Employee indexes on `(department, joined_on, emp_code)` and `(joined_on, department)` support department and headcount queries. I skipped a `half_day` index because no endpoint filters by it.

2. **Concurrent punch-ins.** Both requests try to insert a record. The unique employee/date index allows one insert, so that request gets 201. The other raises `DuplicateKeyError`, which I catch and return as 409. Checking for an existing record before inserting would still leave a race.

3. **Ties.** `$rank` gives equal totals the same rank, for example: 1, 2, 2, 4. Filtering by `rank <= limit` includes everyone tied at the cutoff. Employee code determines display order within each tie.

4. **Employees with no logs.** The department query starts with employees who joined by month-end. They count toward headcount even when the attendance lookup finds no logs.

5. **100 times the data.** I would measure the slow queries first. For repeated reports, I would consider daily department summaries and update them when attendance is corrected.
