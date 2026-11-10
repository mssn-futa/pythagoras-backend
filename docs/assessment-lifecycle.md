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
Triggers (below) add: submission quiz/student equal its attempt's, no new
legacy rows, insert-only submissions, frozen attempt timing.
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
Quiz:    draft -> published -> closed (terminal); only via publish_quiz/close_quiz
Attempt: in_progress -> submitted | expired (now >= expires_at, no grade)
```

**Content lock (D3 + lead decision M4).** A quiz's questions and options are
frozen once the quiz is published/closed **or has any attempt or submission,
legacy included, even while it is a draft**. Admins author a new quiz instead.
No add/edit/delete/reorder.

Python guards (raise `quiz.errors`):

- `Quiz.save`: created only as a draft with no `published_at`/`closed_at`.
  `status`, `published_at` and `closed_at` change only with the single-use
  internal token that `publish_quiz`/`close_quiz` set
  (`quiz._lifecycle_transition`), so `save()` cannot publish an unvalidated
  quiz or close it (`InvalidTransition`). Once not draft, `Quiz.LOCKED_FIELDS`
  (`type`, `time_limit`, `deadline`, `related_event`, `related_course`) are
  `QuizImmutable` with per-field `fields`; title/description stay editable.
  `Quiz.objects...update()` refuses lifecycle keys and locked fields of
  non-draft rows.
- `Quiz.delete`: draft only (`QuizImmutable`); attempts/submissions PROTECT it.
- `Question`/`Option` `save`/`delete` and their querysets' `update`/`delete`/
  `bulk_create`/`bulk_update` (old and new quiz on a move): `QuizImmutable`
  when content-locked. `assert_content_editable()` opens its own
  `transaction.atomic()` and locks the quiz row `FOR NO KEY UPDATE`.
- `Attempt.save`: quiz/student/started_at/expires_at frozen; a submitted or
  expired attempt cannot change. `Attempt.objects.update()`/`delete()` and
  `Attempt.delete()` refuse.
- `Submission`: insert-only (`save()` on an existing row, `delete()`,
  queryset `update()`/`delete()` raise `SubmissionImmutable`); must match its
  attempt's quiz/student (`ValueError`); `is_legacy=True` inserts refused.

Database triggers (`quiz.0007`, every path including raw SQL; Django raises
`IntegrityError`, `quiz.views.domain_errors` maps `23Q01` to 409
`QUIZ_IMMUTABLE`; `quiz.errors.is_immutable_db_error(exc)` for other callers):

| Table | Refused | SQLSTATE |
| --- | --- | --- |
| `quiz_question`, `quiz_option` | INSERT/UPDATE/DELETE when the old or new parent quiz is content-locked | `23Q01` |
| `quiz_quiz` | non-draft: changes to type/time_limit/deadline/related_event_id/related_course_id/published_at; any status/closed_at change except published -> closed; DELETE | `23Q01` |
| `quiz_attempt` | changes to quiz_id/student_id/started_at/expires_at; any change once submitted/expired; DELETE | `23Q01` |
| `quiz_submission` | UPDATE, DELETE, INSERT with `is_legacy` | `23Q01` |
| `quiz_submission` | INSERT whose quiz/student differ from its attempt's | `23Q02` |

TRUNCATE does not fire row triggers (test flushes, operator restores).

## Time, arrival and locking

- `server_now()` is PostgreSQL `transaction_timestamp()`: the start of the
  transaction the service runs in. That is the request's **arrival**: when a
  view wraps the call in `atomic()`, the request transaction start; otherwise
  the service's own transaction, whose first statement reads it before any lock
  is requested. Expiry (`now >= expires_at`) and availability are judged on
  arrival, so lock waits never turn an on-time submission into
  `ATTEMPT_EXPIRED`. All timestamps written in one transaction are that one
  instant.
- publish/close: quiz row `FOR UPDATE`.
- start/finalize: quiz row `FOR SHARE`, so
  students run in parallel, while still conflicting with publish/close and
  content writes; finalize then locks only the attempt row
  (`FOR UPDATE OF "quiz_attempt"`, not the joined user row). One attempt per
  student is decided by the unique constraint: a racing insert waits for the
  winner, then reuses its (locked) row.
- question/option writes: quiz row `FOR NO KEY UPDATE`.
- Lock order: quiz row, then attempt row.
- Resolved edge (ASSESS-04): a write of `expired` committed while an
  earlier-arriving finalize waits for the attempt lock would make that
  finalize fail. `finalize_attempt` therefore never persists `expired` (a late
  request only raises `AttemptExpired`), no API path calls `mark_expired`, and
  reads use `effective_attempt_status`. `mark_expired` stays an operator
  primitive for rows well past expiry.

## Service primitives (`quiz.lifecycle`, `quiz.services`)

| Primitive | Use |
| --- | --- |
| `server_now()` | arrival time (`transaction_timestamp()`) |
| `compute_expires_at(started_at, time_limit, deadline)` | A10 `min(start + limit, deadline)` |
| `is_expired(attempt, now)` / `effective_attempt_status(attempt, now)` | `now >= expires_at` is expired |
| `quiz_is_available(quiz, now)` / `available_quizzes(qs=None)` | published and `deadline > now` (DB `Now()`) |
| `ordered_questions(quiz, mcq_only=True)` | `(order, id)` with options prefetched in order |
| `publication_errors(quiz, now)` | field errors; empty quiz, timing, MCQ-only, 4 distinct non-empty options, exactly one correct, points >= 1, total <= `MAX_STORED_POINTS` |
| `publish_quiz(quiz_id)` | locks, validates, `(quiz, changed)`; idempotent for published; `PublicationInvalid` / `InvalidTransition` |
| `close_quiz(quiz_id)` | `(quiz, changed)`; idempotent; draft is `InvalidTransition` |
| `assert_quiz_draft(quiz)` / `assert_quiz_changes_allowed(quiz, fields)` | quiz authoring guards for API validation |
| `quiz.models.quiz_content_locked(id)` / `assert_content_editable(*ids)` | question/option content lock (M4) |
| `delete_draft_quiz(quiz_id)` | `QuizReferenced` (history) before `QuizImmutable` (not draft) |
| `is_eligible_student(user)` | active `role=student` (no enrollment gate) |
| `share_lock_quiz(id)` / `lock_attempt(**lookup)` / `create_attempt(quiz, student, now)` | locking building blocks |
| `start_attempt(quiz_id, student)` | returns existing attempt unchanged first; a draft is `Quiz.DoesNotExist` (404) next; legacy submission is `SubmissionConflict`; else requires availability at arrival (`QuizUnavailable`); `(attempt, created)` |
| `finalize_attempt(attempt_id, student, answers)` | ownership (`Attempt.DoesNotExist`), replay/`SubmissionConflict`, expiry at arrival (`AttemptExpired`; not persisted), `InvalidAnswers`, then atomic grade + snapshot + attempt `submitted`; `(submission, created)` |
| `mark_expired(attempt_id)` | persist expiry without a grade (operator use only; see the resolved edge) |
| `replay_or_conflict(submission, answers)` | strict JSON comparison |
| `finalize_locked(quiz, attempt, answers, now)` | lower-level step under caller-held locks |
| `QuizService.validate_answers` / `QuizService.grade` | answer membership; `GradingResult(score, total_points, snapshot)` |

Tests may pass `now=`; API code must not.

Error codes (`quiz.errors`, all `code` attributes; map with
`quiz.views.domain_errors`): `QUIZ_IMMUTABLE` (409), `INVALID_TRANSITION`
(409), `RESOURCE_REFERENCED` (409), `QUIZ_UNAVAILABLE` (409 on a new start; a draft is 404),
`ATTEMPT_EXPIRED` (409), `SUBMISSION_CONFLICT` (409), `PublicationInvalid` ->
400 `VALIDATION_ERROR` with `fields`.

Frozen student attempt contract: `quiz.serializers.StudentAttemptSerializer`
returns `id, quiz_id, quiz_title, status, started_at, expires_at, server_time,
submission_id` (`status` effective at `server_time`; `submission_id` null
until finalized). Questions are delivered by the ASSESS-04 view.

## Existing API changes

The authoring/publication/closing API (ASSESS-03) supersedes the temporary
quiz/question write behaviour described here in ASSESS-02; see
[assessment-authoring.md](assessment-authoring.md). The attempt, submission and
results API (ASSESS-04) supersedes the temporary direct submit; see
[assessment-results.md](assessment-results.md).

- Student visibility is `published` and `deadline > now`, not `is_active`.
- The ASSESS-02 direct submit (`POST /quiz/submissions/ {"quiz", "answers"}`,
  which created and finalized an attempt in one step) is retired by ASSESS-04:
  that body is a 400 and `QuizService.submit` no longer exists.

## Migrations and existing data

| Migration | Effect |
| --- | --- |
| `quiz.0004_assessment_lifecycle_schema` | Preflight first: aborts, changing nothing, if a student has several submissions for one quiz (lists IDs). Adds fields/Attempt; existing submissions get `is_legacy=true`; widens score; FKs to PROTECT. |
| `quiz.0005_assessment_legacy_data` | Every quiz stays `draft`; `legacy_is_active = is_active`, `is_active = false`. Question/option `order` = 1..n by ID within quiz/question. |
| `quiz.0006_assessment_lifecycle_constraints` | NOT NULL order, auto timestamps, all constraints above. |
| `quiz.0007_assessment_history_triggers` | PostgreSQL trigger functions/triggers above. No data change. |

Effect: previously active quizzes disappear from students until an admin
publishes them through validation (D3 "legacy quizzes to draft"). Non-MCQ
questions are preserved but block publication. A legacy quiz with legacy
submissions is content-locked (M4): it can be published if its existing graph
is valid, otherwise replaced by a new quiz. A student with a legacy
submission for a quiz has used their attempt (`SubmissionConflict` on start). Legacy grades, answers and
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

Rollback limits: `0007` (and `0006` behind it) refuses to unapply while any
attempt or release (non-legacy) submission exists (restore the pre-upgrade
backup instead); with no such history, unapplying `0007` drops the triggers.
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
