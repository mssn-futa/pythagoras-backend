# Assessment lifecycle, attempts and history (ASSESS-02)

Governing design: D3 (domain model, lifecycle, migration) under D0 A08-A12.
This is the model/service foundation for ASSESS-03 (authoring/publication API)
and ASSESS-04 (attempt start/submit/results API). No new endpoints are added.

## Models and fields

| Model | Field | Notes |
| --- | --- | --- |
| `Quiz` | `status` | `draft` / `published` / `closed` (`Quiz.Status`), default `draft` |
| | `deadline` | nullable; published/closed requires non-null (DB check) and publish requires future |
| | `time_limit` | minutes; 1-180 required to publish (validators + DB check when not draft) |
| | `is_active` | retained, **derived**: `status == published` on every save and by DB check; not writable |
| | `published_at`, `closed_at` | server time of the transition; DB checks tie them to status |
| | `created_at`, `updated_at` | auto; **NULL for pre-ASSESS-02 rows** (unknown, not invented) |
| | `legacy_is_active` | pre-upgrade `is_active` of legacy rows; NULL for newer quizzes |
| | `type` | legacy metadata retained; new drafts default `arabic`; no rule depends on it |
| | `related_event`, `related_course` | `on_delete=PROTECT` |
| `Question` | `order` | 1-based, unique per quiz (deferred unique), order of every payload `(order, id)`; auto-appended when omitted |
| `Option` | `order` | 1-based, unique per question (deferred unique); auto-appended when omitted |
| `Attempt` (new) | `quiz`, `student` | `PROTECT`; unique `(quiz, student)` = `quiz_attempt_one_per_student` |
| | `status` | `in_progress` / `submitted` / `expired` (`Attempt.Status`) |
| | `started_at`, `expires_at` | server-set at start, frozen; DB check `expires_at > started_at` |
| | `finalized_at` | set iff `submitted`; DB check `started_at <= finalized_at < expires_at` |
| `Submission` | `attempt` | one-to-one, `PROTECT`, NULL only for legacy rows |
| | `score`, `total_points` | `integer` (widened from smallint); DB check `score <= total_points` |
| | `grading_snapshot` | JSON, see below; NULL only for legacy rows |
| | `is_legacy` | true for every row that existed before the upgrade |
| | `quiz`, `student` | `PROTECT`; unique `(quiz, student)` = `quiz_submission_one_per_student` |
| | `submitted_at`, `graded_at` | release rows: same server instant as `attempt.finalized_at` |

DB check `quiz_submission_release_shape`: either `is_legacy` with no attempt,
or not legacy with attempt, `total_points`, `grading_snapshot` and `graded_at`.
`Course`/`Enrollment` are unchanged.

Grading snapshot (version 1, immutable, contains no correct-option IDs):

```json
{"version":1,"quiz_id":45,"quiz_title":"Arabic week 1","score":2,"total_points":5,
 "questions":[{"question_id":10,"order":1,"points":2,"selected_option_id":102,"awarded_points":2},
              {"question_id":11,"order":2,"points":3,"selected_option_id":null,"awarded_points":0}]}
```

Per-question `awarded_points` reveals right/wrong per question; whether students
see it is an ASSESS-04 decision (A12 forbids only answer keys).

## Lifecycle and guards

```text
Quiz:    draft -> published -> closed (terminal); same-state saves allowed
Attempt: in_progress -> submitted | expired (now >= expires_at, no grade)
```

Model guards (raise `quiz.errors`):

- `Quiz.save`: created only as draft (`InvalidTransition`); transitions per
  `Quiz.TRANSITIONS`; once not draft, `Quiz.LOCKED_FIELDS` (`type`,
  `time_limit`, `deadline`, `related_event`, `related_course`) are
  `QuizImmutable` with per-field `fields`; title/description stay editable.
- `Quiz.delete`: draft only (`QuizImmutable`); attempts/submissions PROTECT it.
- `Question`/`Option` save/delete: the quiz (old and new on a move) must be a
  draft (`QuizImmutable`). Inside a transaction they lock the quiz row
  `FOR NO KEY UPDATE`, which serializes with publish/close (`FOR UPDATE`).
- `Attempt.save`: quiz/student/started_at/expires_at frozen; a submitted or
  expired attempt cannot change. `Attempt.delete` and queryset delete refuse.
- `Submission`: insert-only; `save()` on an existing row, `delete()`, and
  queryset `update()`/`delete()` raise `SubmissionImmutable`.

Bulk `QuerySet.update` on Quiz/Question/Option and raw SQL bypass model guards
(DB checks still hold); release code must use `quiz.lifecycle`.

## Service primitives (`quiz.lifecycle`, `quiz.services`)

| Primitive | Use |
| --- | --- |
| `server_now()` | DB clock (`statement_timestamp()`); every lifecycle timestamp |
| `compute_expires_at(started_at, time_limit, deadline)` | A10 `min(start + limit, deadline)` |
| `is_expired(attempt, now)` / `effective_attempt_status(attempt, now)` | `now >= expires_at` is expired |
| `quiz_is_available(quiz, now)` / `available_quizzes(qs=None)` | published and `deadline > now` (DB `Now()`) |
| `ordered_questions(quiz, mcq_only=True)` | `(order, id)` with options prefetched in order |
| `publication_errors(quiz, now)` | field errors; empty quiz, timing, MCQ-only, 4 distinct non-empty options, exactly one correct, points >= 1, total <= `MAX_STORED_POINTS` |
| `publish_quiz(quiz_id)` | locks, validates, `(quiz, changed)`; idempotent for published; `PublicationInvalid` / `InvalidTransition` |
| `close_quiz(quiz_id)` | `(quiz, changed)`; idempotent; draft is `InvalidTransition` |
| `assert_quiz_draft(quiz)` / `assert_quiz_changes_allowed(quiz, fields)` | authoring guards for API validation |
| `delete_draft_quiz(quiz_id)` | `QuizReferenced` (history) before `QuizImmutable` (not draft) |
| `is_eligible_student(user)` | active `role=student` (no enrollment gate) |
| `start_attempt(quiz_id, student)` | locks quiz; returns existing attempt unchanged first; legacy submission is `SubmissionConflict`; else requires availability (`QuizUnavailable`); `(attempt, created)` |
| `finalize_attempt(attempt_id, student, answers)` | ownership (`Attempt.DoesNotExist`), replay/`SubmissionConflict`, expiry (`AttemptExpired`, persists `expired`), `InvalidAnswers`, then atomic grade + snapshot + attempt `submitted`; `(submission, created)` |
| `mark_expired(attempt_id)` | persist expiry without a grade |
| `replay_or_conflict(submission, answers)` | strict JSON comparison |
| `finalize_locked(quiz, attempt, answers, now)` | lower-level step under caller-held locks |
| `QuizService.validate_answers` / `QuizService.grade` | answer membership; `GradingResult(score, total_points, snapshot)` |

Lock order: quiz row, then attempt row. All writers open `transaction.atomic()`.
Tests may pass `now=`; API code must not.

Error codes (`quiz.errors`, all `code` attributes; map with
`quiz.views.domain_errors`): `QUIZ_IMMUTABLE` (409), `INVALID_TRANSITION`
(409), `RESOURCE_REFERENCED` (409), `QUIZ_UNAVAILABLE`, `ATTEMPT_EXPIRED`
(409), `SUBMISSION_CONFLICT` (409), `PublicationInvalid` -> 400
`VALIDATION_ERROR` with `fields`.

Frozen student attempt shape: `quiz.serializers.StudentAttemptSerializer`
(`id, quiz_id, status, started_at, expires_at, questions[id,text,points,order,
options[id,text,order]]`; the ASSESS-04 view adds `server_time`).

## Existing API changes (temporary paths, until ASSESS-03/04)

- Student visibility is `published` and `deadline > now`, not `is_active`.
- `POST/PUT /quiz/quizzes/`: `status`/`is_active` are read-only; sending a
  different value is a 400 field error. Published/closed quizzes accept only
  title/description (else 409 `QUIZ_IMMUTABLE`). `deadline` is writable.
- Question writes only on draft quizzes (409 `QUIZ_IMMUTABLE`); `order` is
  optional and validated for uniqueness. Payloads include `order`, `status`,
  `deadline`, `published_at`, `closed_at`, `created_at`, `updated_at`.
- `DELETE /quiz/quizzes/{id}/`: 409 `RESOURCE_REFERENCED` with
  attempts/submissions, 409 `QUIZ_IMMUTABLE` if not draft.
- `POST /quiz/submissions/` (direct submit) keeps its request/response and
  promises; it now creates the student's attempt at the server instant and
  finalizes it in the same transaction (`started_at == finalized_at`), storing
  `total_points` and a snapshot. An existing expired attempt yields 409
  `ATTEMPT_EXPIRED`.
- There is no publish endpoint until ASSESS-03; publish via
  `quiz.lifecycle.publish_quiz` (e.g. `manage.py shell`).

## Migrations and existing data

| Migration | Effect |
| --- | --- |
| `quiz.0004_assessment_lifecycle_schema` | Preflight first: aborts, changing nothing, if a student has several submissions for one quiz (lists IDs). Adds fields/Attempt; existing submissions get `is_legacy=true`; widens score; FKs to PROTECT. |
| `quiz.0005_assessment_legacy_data` | Every quiz stays `draft`; `legacy_is_active = is_active`, `is_active = false`. Question/option `order` = 1..n by ID within quiz/question. |
| `quiz.0006_assessment_lifecycle_constraints` | NOT NULL order, auto timestamps, all constraints above. |

Effect: previously active quizzes disappear from students until an admin
publishes them through validation (D3 "legacy quizzes to draft"). Non-MCQ
questions are preserved but block publication. Legacy grades, answers and
timestamps are preserved; no attempts, denominators or snapshots are
fabricated.

Procedure for an existing database (after a backup):

```sh
python manage.py assessment_preflight   # exit 0: "No duplicate submissions found."
python manage.py migrate --noinput
```

On exit 1 it prints `duplicate submissions: quiz Q student S: submission IDs
A, B` plus non-blocking notes. Resolve with lead approval; never delete
automatically. Rerun until it passes.

Rollback limits: `0006` refuses to unapply while any attempt or release
(non-legacy) submission exists (restore the pre-upgrade backup instead).
Reversing `0004` narrows `score` to smallint and fails if any score exceeds
32767. Reversing `0005` restores `is_active` from `legacy_is_active` for
quizzes still in draft and from status otherwise.

## Seams

- dawah: `Quiz.related_event` is PROTECT, so deleting a referenced event raises
  `ProtectedError`. DAWAH-01's `dawah.services.delete_event` maps it (and its
  pre-check of `Event._meta.related_objects`) to 409 `RESOURCE_REFERENCED`.
  Until that lands, the legacy dawah delete view surfaces it as a 500 while the
  event, quiz and history survive.
- Users with attempts/submissions cannot be deleted (PROTECT); deactivate them.
