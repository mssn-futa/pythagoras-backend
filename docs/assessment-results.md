# Assessment attempts, submissions and results API (ASSESS-04)

Governing design: D3 "Backend Design", "API Contracts" and invariants under D0
(A09 one attempt, A10 server timing, A12 no answer keys; envelope, pagination,
`ATTEMPT_EXPIRED`/`SUBMISSION_CONFLICT`) and the D5 throttles. Builds on the
ASSESS-02 primitives ([assessment-lifecycle.md](assessment-lifecycle.md)) and
the ASSESS-03 authoring API ([assessment-authoring.md](assessment-authoring.md)).
No schema change: the quiz migration chain still ends at `0007`.

Code: `quiz/views.py` (`QuizAttemptStartView`, `AttemptDetailView`,
`UserSubmissionView`, `SubmissionDetailView`, `QuizSubmissionView`),
`quiz/serializers.py` (`StudentAttemptDetailSerializer`,
`StudentSubmissionSerializer`, `AdminSubmissionSerializer`,
`AdminSubmissionDetailSerializer`, `SubmissionCreateSerializer`),
`quiz/lifecycle.py` (`start_attempt`, `finalize_attempt`), `quiz/throttles.py`.

## Endpoints

| Route | Method | Who | Behaviour |
| --- | --- | --- | --- |
| `/quiz/quizzes/{id}/attempts/` | POST | active student | Start (201) or return the existing attempt unchanged (200), also after closing/deadline/expiry. New attempt: a draft/missing quiz is 404, checked before anything else so a draft looks missing to every student; then a legacy submission is 409 `SUBMISSION_CONFLICT`; then a closed/past-deadline quiz is 409 `QUIZ_UNAVAILABLE`. No body (`{}` accepted). 20/min per student. |
| `/quiz/attempts/{id}/` | GET | active student | Own attempt only (other/missing 404) with key-free questions and `server_time`. |
| `/quiz/submissions/` | POST | active student | Finalize own attempt: `{"attempt_id", "answers"}`. 201 graded; identical replay 200; changed 409 `SUBMISSION_CONFLICT`; first arrival at/after expiry 409 `ATTEMPT_EXPIRED`; 400 validation; 404 foreign/missing attempt. 60/min per student. |
| `/quiz/submissions/` | GET | active user | Own results, newest first (`-submitted_at`, `-id`), D0-paginated, filters `attempt`, `quiz` (positive integers, else 400). Admins have none of their own. |
| `/quiz/submissions/{id}/` (+ no-slash alias) | GET | active user | Student: own result (else 404). Admin: any result, review payload. No mutation methods (405). |
| `/quiz/quizzes/{id}/submissions/` | GET | admin | Flat paginated result rows of one quiz, newest first. Missing quiz 404. |

Anonymous is 401, an inactive account 403 `ACCOUNT_INACTIVE`, a non-student on a
student route 403 `PERMISSION_DENIED` (`is_staff` grants nothing; a staff
student is a student).

Order of checks for a submit: authentication/permission, throttle, request
structure (400), attempt ownership (404), existing finalization (replay 200 /
409 conflict, before expiry so an accepted result is always replayable),
expiry at arrival (409), answer membership (400), then grading and the atomic
write of submission, score, denominator, snapshot and attempt status.

## Payloads

Attempt (start, resume, GET): the frozen ASSESS-02 contract `id, quiz_id,
quiz_title, status, started_at, expires_at, server_time, submission_id` plus
`questions[{id, text, points, order, options[{id, text, order}]}]` (MCQ only,
persisted `(order, id)` order). `status` is effective at `server_time`
(database clock): `in_progress`, `submitted`, or `expired` for an unfinalized
attempt at/after `expires_at`. `quiz_title` is the current title.

```json
{"success":true,"data":{"id":1,"quiz_id":1,"quiz_title":"Arabic week 1","status":"in_progress","started_at":"2026-11-10T23:35:15.119396Z","expires_at":"2026-11-11T00:05:15.119396Z","server_time":"2026-11-10T23:35:15.124316Z","submission_id":null,"questions":[{"id":1,"text":"Question 1","points":2,"order":1,"options":[{"id":1,"text":"A","order":1},{"id":2,"text":"B","order":2},{"id":3,"text":"C","order":3},{"id":4,"text":"D","order":4}]}]},"message":"Attempt started."}
```

Submit request (no student, score or timing keys; any other key is 400):

```json
{"attempt_id":1,"answers":{"1":2,"2":5}}
```

`answers` keys are canonical decimal question-ID strings of the attempt's quiz,
values the integer ID of one option of that question. Missing questions score
zero. Rejected (400, `fields.answers.<key>`): non-object, unknown/foreign/
non-canonical keys (`"01"`, `" 1"`, `"1.0"`), booleans/strings/floats/lists,
foreign options, legacy non-MCQ questions. One bad answer rejects the whole
request and leaves the attempt unconsumed.

Student result (submit, replay, history rows, detail). Lead decision after
ASSESS-06 plus D3's `status`; no answers, snapshot or keys:

```json
{"success":true,"data":{"id":1,"quiz_id":1,"quiz_title":"Arabic week 1","attempt_id":1,"status":"submitted","score":2,"total_points":5,"submitted_at":"2026-11-10T23:35:15.155132Z","graded_at":"2026-11-10T23:35:15.155132Z"},"message":"Result saved."}
```

Admin row (lead decision after ASSESS-05): the result fields plus
`student_id`, `student_name`, `student_email`, `is_legacy`. Admin detail adds
`answers`, `grading_snapshot` and the attempt's `started_at`/`expires_at`
(correct options are on the admin quiz detail, not in results):

```json
{"success":true,"data":{"id":1,"quiz_id":1,"quiz_title":"Arabic week 1","attempt_id":1,"status":"submitted","score":2,"total_points":5,"submitted_at":"2026-11-10T23:35:15.155132Z","graded_at":"2026-11-10T23:35:15.155132Z","student_id":2,"student_name":"Amina Yusuf","student_email":"amina@example.test","is_legacy":false,"answers":{"1":2,"2":5},"grading_snapshot":{"version":1,"quiz_id":1,"quiz_title":"Arabic week 1","score":2,"total_points":5,"questions":[{"question_id":1,"order":1,"points":2,"selected_option_id":2,"awarded_points":2},{"question_id":2,"order":2,"points":3,"selected_option_id":5,"awarded_points":0}]},"started_at":"2026-11-10T23:35:15.119396Z","expires_at":"2026-11-11T00:05:15.119396Z"},"message":"Result retrieved."}
```

Errors:

```json
{"success":false,"data":{"code":"ATTEMPT_EXPIRED"},"message":"This attempt has expired."}
{"success":false,"data":{"code":"SUBMISSION_CONFLICT"},"message":"Different answers were already submitted for this attempt."}
{"success":false,"data":{"code":"QUIZ_UNAVAILABLE"},"message":"This quiz is not available."}
{"success":false,"data":{"code":"VALIDATION_ERROR","fields":{"answers":{"1":["Not an option of this question."]}}},"message":"Correct the highlighted fields."}
```

## Grading, denominator and legacy rows

- Weighted: each MCQ question awards its persisted `points` when the selected
  option is its single persisted correct option; `total_points` is the sum of
  the quiz's MCQ question points (legacy text questions of a migrated quiz are
  not counted and cannot be answered).
- Results always report the stored `score`/`total_points` (equal to the
  snapshot); nothing is regraded on read. Published content is frozen, so the
  graph graded against is the one the attempt showed.
- `quiz_title` of a result is the title in its snapshot (as graded); title
  corrections never rewrite results. Legacy rows without a snapshot show the
  current title.
- Legacy (pre-ASSESS-02) submissions appear in history and admin rows with
  `attempt_id: null`, `total_points: null`, `graded_at` as stored and
  `is_legacy: true` (admin). No denominator or snapshot is invented; clients
  show the score without a denominator.
- Expired attempts have no result row and no grade (D3).

## Time, replay and expiry

- Start and finalize judge time on arrival (`server_now()`, the transaction
  start on the PostgreSQL clock). The first submission must arrive strictly
  before `expires_at`; there is no grace period.
- **Expiry is never persisted by a request.** A late submit returns
  `ATTEMPT_EXPIRED` and leaves the row `in_progress`; reads use
  `effective_attempt_status`. This closes the ASSESS-02 open edge: a late
  arrival holding the attempt lock can no longer write `expired` and reject an
  earlier, on-time arrival queued behind it (tested in
  `LateArrivalLockOrderTests`). `mark_expired` remains an operator primitive
  only.
- Replay: an identical answer map (strict JSON comparison of the stored,
  validated map; key order irrelevant, `1` vs `1.0`/`true` different) returns
  200 with the saved result at any time, also after expiry or closing. Any
  other map is 409 and never overwrites. Clients keep the frozen payload and
  resend it after an uncertain outcome (network loss, 5xx, 429), or read
  `GET /quiz/attempts/{id}/` (`submission_id`) or
  `GET /quiz/submissions/?attempt={id}`; they never start a new attempt.
- Closing blocks new attempts only; started attempts keep their expiry and can
  be submitted until it.

## Concurrency

Start: quiz row `FOR SHARE`, unique `(quiz, student)` decides racing inserts
(one 201, the rest 200 with the same attempt). Finalize: quiz `FOR SHARE`, then
the attempt row `FOR UPDATE`; the first holder grades and commits, waiters see
its submission and replay (200) or conflict (409). Any failure rolls back
submission and attempt together (no partial grade). Tests:
`quiz/tests/test_concurrency.py` (threaded API and service races, PostgreSQL).

## Retired direct submission (decision)

ASSESS-01/02's `POST /quiz/submissions/ {"quiz", "answers"}` created and
finalized an attempt in one step (`started_at == finalized_at`), which would
bypass the timed attempt. It is **retired**, not rerouted: a body with `quiz`
is 400 `fields.quiz` ("Direct quiz submission was retired. Start an attempt
with POST /quiz/quizzes/{id}/attempts/ and submit its attempt_id."), creating
nothing, and `QuizService.submit` is removed. Rerouting `{quiz}` to the
student's existing attempt was rejected: it would keep a second request shape
alive for no current client (ASSESS-06 sends `attempt_id`).

## Throttles

`quiz.throttles`: attempt start 20/min (`quiz_attempt_start`) and submissions
60/min (`quiz_submission`) per authenticated student (user ID, not IP, so a
shared NAT does not pool students). Override with
`REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'][scope]`; no settings entry is needed
for the defaults. Reads are not throttled; unauthorized requests do not spend a
budget. 429 carries `Retry-After`. Counters use the default cache: deployment
needs a shared cache for cross-worker enforcement (D5).

## Tests

`quiz/tests/test_attempts_api.py` (start/resume/availability, attempt reads and
effective status, full journey, history ordering/filters/pagination with legacy
rows, admin rows/detail, recursive key walk over every student payload and
error, throttles, OpenAPI wire contract and examples),
`quiz/tests/test_submissions.py` (grading, forged keys, malformed input,
retired path, rollback, replay/conflict, exact expiry boundary),
`quiz/tests/test_concurrency.py`, plus updated permission/serialization suites.
