# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `init_db()` | Missing database indexes for unique constraints and sorting performance. | Duplicate records allowed; queries suffer collection scans (`COLLSCAN`). | Added unique index on `emp_code`, compound index `(emp_code, date)` on `attendance_logs`, and secondary indexes on `department`, `date`, `status`. |
| 2 | `calculate_attendance_date()` | Incorrect handling of overnight shifts where punch-in occurs after midnight (e.g. 01:00 AM). | Overnight punch-in logged under the next calendar date instead of the shift's start date. | Implemented R1 logic checking `if p_time < end_t` for overnight shifts (`shift_end <= shift_start`). |
| 3 | `calculate_late_minutes()` | Applied grace period subtraction or miscalculated grace period edge case. | Punching in at 09:40:01 yielded incorrect late minutes. | Updated logic: if `diff_sec > 600`, return `diff_sec // 60` from shift start (R2). |
| 4 | `punch_in()` | Race condition on concurrent punch-ins for the same employee and date. | Concurrent HTTP requests created duplicate punch-in entries for the same date. | Enforced a compound unique index on `(emp_code, date)` and handled `DuplicateKeyError` returning 409. |
| 5 | `punch_out()` | Non-atomic check-and-update led to race condition. | Two parallel punch-out requests both succeeded (200 OK). | Replaced fetch-then-update with atomic `find_one_and_update` checking `punch_out: None`. |
| 6 | `compute_derived_fields()` | Used standard Python float rounding (`round()`), which uses Banker's rounding. | Rounding `.005` rounded down to `.00` instead of up to `.01`. | Replaced with explicit standard arithmetic half-up rounding (`round_half_up`). |
| 7 | `list_attendance()` | Missing filter validation when `date_from > date_to`. | Invalid date range returned empty result with status 200 instead of validation error. | Added explicit validation raising HTTP 422 if `date_from > date_to`. |
| 8 | `list_employees()` | Pagination total returned total collection count instead of total filtered count. | Filtered query with `department` parameter returned wrong total count. | Updated total computation to `count_documents(query)`. |
| 9 | Boundary conversion | Returned naive datetimes without UTC conversion. | Timestamp offsets differed across system locales. | Ensured all datetimes are stored tz-aware UTC and converted to epoch ms at boundaries. |
| 10 | `/health` | Always returned 200 even when MongoDB connection failed. | Application appeared healthy during database outages. | Added explicit `db.command("ping")` call, raising HTTP 503 on failure. |

### Validated Non-Defects
- **`emp_code` primary identifier:** Correctly uses string business key rather than MongoDB `ObjectId`.
- **Epoch millisecond validation range:** Default Pydantic range `[100000000000, 4102444800000]` correctly filters invalid timestamps.