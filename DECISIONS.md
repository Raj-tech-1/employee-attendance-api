# DECISIONS.md

1. **Indexes.**
   Created indexes:
   - `employees.emp_code` (unique): Enforces customer code uniqueness and fast employee lookup.
   - `employees.department`: Optimizes department summary and trend pipelines.
   - `attendance_logs.(emp_code, date)` (unique): Prevents duplicate attendance logs per day and speeds up individual record fetches.
   - `attendance_logs.(date, emp_code)`: Backs the default listing query and range-based analytics.
   Rejected index: An index on `history.at` was considered but rejected because history array searches are not part of hot query paths.

2. **Punch-in race.**
   When two identical punch-in requests arrive simultaneously, both compute the target IST date and attempt to insert a document into `attendance_logs`. MongoDB's unique index on `(emp_code, date)` allows exactly one document insertion to succeed (returning 201 Created), while the second operation fails with a `DuplicateKeyError`, caught by FastAPI and converted to HTTP 409 Conflict.

3. **Ties.**
   The late leaderboard uses MongoDB's `$setWindowFields` with the `$rank` function. Tied employees share the same rank (e.g., ranks 1, 2, 2, 4). `limit` is applied on the calculated `rank` (`$lte: limit`), so all tied candidates at or below the cutoff rank are included. Tied rows are sub-sorted ascending by `emp_code`.

4. **Headcount.**
   Department summary begins from the `employees` collection filtered by `joined_on <= end_of_month`. It performs a `$lookup` to join `attendance_logs`. Unwinding with `preserveNullAndEmptyArrays: True` ensures employees with zero logs in the month are preserved in `$group` headcount calculation.

5. **One thing you would change at 100x scale.**
   I would pre-aggregate daily and monthly attendance stats using transactional outbox triggers or scheduled background workers into a dedicated read-optimized analytics collection, eliminating runtime aggregation pipelines over raw log documents.