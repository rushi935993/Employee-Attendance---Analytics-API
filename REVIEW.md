# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `EmployeeIn.shifts_differ` | Rejecting equal shift start/end conflicts with the contract's explicit `shift_end <= shift_start` overnight rule. | POST an employee with `shift_start` and `shift_end` both `22:00`; contract treats it as overnight rather than rejecting it. | Removed the extra restriction; the API now follows the stated `<=` rule. |
| 2 | `RegularizeIn.has_edit` | `Optional` fields accepted explicit JSON `null`, although a supplied editable field must be a valid value; explicit null could make status null or clear a time unexpectedly. | PATCH `{"status": null, "reason":"...", "regularized_by":"..."}`. | Reject explicit null for supplied editable fields; omitted fields remain unchanged. |
| 3 | `regularize` timestamp normalization | PyMongo normally returns naive UTC datetimes. The comparison normalized the old value to aware UTC but could leave the new unchanged value naive, making unchanged punch times appear changed in the audit record. | PATCH a record changing only its status and inspect `history.changes`; unchanged punch times must not be listed. | Normalize stored punch timestamps to UTC-aware datetimes before deriving fields and computing the change set. |
| 4 | `ensure_indexes` | The starter lacked a leading `joined_on` index for headcount/date eligibility queries without a department filter and a date/late-minutes index for the leaderboard's range filter. | Inspect `explain("executionStats")` for month analytics on a large dataset. | Added indexes for joined-on scans and late leaderboard date/late-minute filtering. |
| 5 | `explain` | The original explain endpoint returned a bare explain document instead of the contract's `{endpoint, collection, explain}` envelope; several branches explained a simplified query rather than the endpoint's filtered operation. Its date filters were strings, so malformed dates were not consistently rejected. | Call `/admin/explain/attendance_list` and inspect the response shape; pass an invalid `date_from`; compare analytics filters with the explained pipeline. | Added the response envelope, typed date query parameters, date-range validation, and index-backed explain pipelines for the relevant query shapes. |

## Examined and retained

- The unique `(emp_code, date)` index is the concurrency guarantee for punch-in; a check-then-insert alone would be unsafe.
- The compare-and-swap filter in regularization is appropriate: a concurrent loser receives 409 rather than silently overwriting an audit entry.
- The punch-out conditional update allows only one simultaneous request to close a record successfully.
- Epoch milliseconds are strict integers and are truncated to whole seconds before calculations, as required by R1.
