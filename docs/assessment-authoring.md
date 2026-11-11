# Assessment authoring, publication and closing API (ASSESS-03)

Governing design: D3 "State / Lifecycle", "Backend Design", "API Contracts"
under D0 (envelope, pagination, status codes) and A08/A11/A12. Builds on the
ASSESS-02 primitives in [assessment-lifecycle.md](assessment-lifecycle.md);
no schema change (the quiz migration chain still ends at `0007`).

Code: `quiz/views.py` (HTTP), `quiz/serializers.py` (`AdminQuizWriteSerializer`,
`AdminQuestionWriteSerializer`, structure only), `quiz/authoring.py` (locked
nested question/option writes, error flattening), `quiz/lifecycle.py`
(`publish_quiz`, `close_quiz`, `publication_errors`, `delete_draft_quiz`,
`assert_quiz_changes_allowed`), `quiz/models.py` (`assert_content_editable`).

## Endpoints

All success bodies use the D0 envelope `{"success":true,"data":...,"message":...}`;
204 bodies are empty. Admin = active account with `role=admin` (`is_staff`
grants nothing). Anonymous is 401, a non-admin write 403, a hidden or missing
object 404.

| Route | Method | Who | Behaviour |
| --- | --- | --- | --- |
| `/quiz/quizzes/` | GET | active user | Paginated (`page`, `page_size` 20/100). Admin: every quiz, `-id`, optional `status=draft\|published\|closed` (other values 400 `fields.status`). Student: available quizzes (published, deadline > now), `id` order. |
| `/quiz/quizzes/` | POST | admin | Create a draft (201). |
| `/quiz/quizzes/{id}/` | GET | active user | Admin detail with questions, options, IDs, `is_correct`, `content_editable`; student detail without keys (available quizzes only). |
| `/quiz/quizzes/{id}/` | PATCH | admin | Partial update. Draft: every writable field. Published/closed: title/description only, else 409 `QUIZ_IMMUTABLE` with `fields`. |
| `/quiz/quizzes/{id}/` | PUT | admin | Legacy: PATCH rules with `title` required. |
| `/quiz/quizzes/{id}/` | DELETE | admin | Draft without attempts/submissions: 204. History: 409 `RESOURCE_REFERENCED`. Not draft: 409 `QUIZ_IMMUTABLE`. |
| `/quiz/quizzes/{id}/questions/` | POST | admin | Create an MCQ question with its four options (201). |
| `/quiz/question/{id}/` | GET | active user | Admin with keys; student only an MCQ question of an available quiz, no keys. |
| `/quiz/question/{id}/` | PATCH | admin | Partial update; sent `options` replace the set. |
| `/quiz/question/{id}/` | PUT | admin | Legacy: PATCH rules with `text` required; `quiz` must be the question's own quiz. |
| `/quiz/question/{id}/` | DELETE | admin | 204 while content-editable. |
| `/quiz/question/` | GET | admin | Legacy paginated list of every question with keys, `(quiz, order, id)`. |
| `/quiz/question/` | POST | admin | Legacy alias of the canonical create with an integer `quiz` key. |
| `/quiz/quizzes/{id}/publish/` | POST | admin | Validate and publish a draft (200). No body. |
| `/quiz/quizzes/{id}/close/` | POST | admin | Close a published quiz (200), idempotent. No body. |

Every detail route keeps its no-slash alias (`/quiz/quizzes/{id}`,
`/quiz/question/{id}`), which runs the same view. Aliases and legacy routes use
the same serializers, locks and services, so none bypasses a rule.

## Requests

Unknown keys are rejected at every level with 400
`"<key>": ["This field cannot be set or changed here."]` (for example `id`,
`questions`, `published_at`, an option `id`). Numbers and booleans must be JSON
integers/booleans/strings (`"30"`, `30.0`, `true` for an integer, or `123` for a
text field are rejected; text fields answer `Not a valid string.`).

Quiz (create/PATCH):

```json
{"title":"Arabic week 1","description":"Weekly assessment","time_limit":30,"deadline":"2026-10-12T20:00:00Z","related_event":null}
```

- `title` required on create, 1-250 chars after trimming; `description` 0-250, default `""`.
- `time_limit` 1-180 or null; `deadline` ISO-8601 or null. Both may be null while
  drafting. The future-deadline rule is checked at publish (D3), not at save.
- `related_event`, `related_course`: integer pk or null; `type`: legacy metadata (default `arabic`). A newly set `related_course` must be a draft or published Arabic class (ASSESS-07, 400 otherwise); an unchanged legacy link is kept. It never restricts quiz eligibility.
- `status`/`is_active`: echoing the current value is tolerated; a different value is 400.

Question (create/PATCH), the D3 body without option IDs:

```json
{"type":"mcq","text":"Question","points":2,"order":1,"options":[{"text":"A","order":1,"is_correct":false},{"text":"B","order":2,"is_correct":true},{"text":"C","order":3,"is_correct":false},{"text":"D","order":4,"is_correct":false}]}
```

- `type` only `mcq` (default). `text` non-empty. `points` integer 1-32767, default 1.
- `order` optional integer 1-10000, unique within the quiz (400 `order`); omitted =
  appended (max + 1, computed under the quiz lock; 400 `order` if that would exceed 10000).
- `options` (required on create): exactly four, non-empty texts (max 250),
  distinct after trim + collapse internal whitespace + casefold, exactly one
  `is_correct: true` (default false), unique option orders 1-10000 (omitted ones
  fill free positions).
- PATCH replaces the option set when `options` is sent; persisted option IDs
  are kept by position (n-th option by order keeps the n-th existing row).
  A question never moves between quizzes. Legacy text/multi questions cannot be
  edited (400 `type`); delete them instead.
- Questions/options change only while the quiz is **content-editable**: a
  draft with no attempt or submission (legacy included). Otherwise 409
  `QUIZ_IMMUTABLE`. Admin payloads expose this as `content_editable`.

## Errors

Validation errors are 400 `VALIDATION_ERROR` whose `fields` are flat dotted
paths with lists of strings (the admin UI joins them):

```json
{"success":false,"data":{"code":"VALIDATION_ERROR","fields":{"points":["Ensure this value is greater than or equal to 1."],"options.2.text":["This field may not be blank."],"options.3.id":["This field cannot be set or changed here."]}},"message":"Correct the highlighted fields."}
```

Option-set rules (count, single correct, distinct, unique orders) are reported
under `options` once every option is individually valid. Publication errors use
`time_limit`, `deadline`, `questions` (empty quiz), `questions.<question id>`
and `total_points`; each `questions.<id>` message is prefixed with the
question's persisted order (`Question <order>: ...`):

```json
{"success":false,"data":{"code":"VALIDATION_ERROR","fields":{"deadline":["The deadline must be in the future."],"questions.10":["Question 1: Exactly 4 options are required."]}},"message":"Correct the highlighted fields."}
```

Conflicts (409):

| Code | When |
| --- | --- |
| `QUIZ_IMMUTABLE` | Locked quiz field changed after publication (`fields` names them); any question/option write on a content-locked quiz; deleting a non-draft quiz. |
| `INVALID_TRANSITION` | Publish of a published or closed quiz; close of a draft. |
| `RESOURCE_REFERENCED` | Deleting a quiz with attempts/submissions. |

Order of checks: authentication/permission, object lookup (404), request
validation (400), then lifecycle/lock state (409) under the quiz row lock.

## Lifecycle semantics

- **Publish** (`lifecycle.publish_quiz`): quiz row `FOR UPDATE`; validates
  timing (1-180 minutes, deadline after the request's arrival instant), at least
  one question, MCQ only, positive points, four distinct non-empty options, one
  correct, total points within storage limits; sets `published_at` to the
  database transaction time. Already published is 409 (not a silent no-op) so
  the UI never reports a stale action as fresh.
- **Close** (`lifecycle.close_quiz`): published -> closed; closed again is 200
  with the original `closed_at` and message `Quiz already closed.`. Blocks new
  attempts; existing attempts keep their expiry and can still be finalized.
- **Metadata correction**: title/description only, after publish or close.
  Unchanged values of locked fields are not changes. Result snapshots keep the
  `quiz_title` at submission (they are insert-only in the database).
- **Concurrency**: question writes lock the quiz row `FOR NO KEY UPDATE` first
  (`assert_content_editable`), publish/close take `FOR UPDATE`, quiz PATCH
  takes `FOR UPDATE`. A write that holds the lock first is validated by the
  publication that follows; a write that waits behind a publication sees the
  published row and gets 409. The `quiz.0007` triggers remain the backstop.

## Example responses

Create draft (201):

```json
{"success":true,"data":{"id":1,"title":"Arabic week 1","type":"arabic","description":"Weekly assessment","related_event":null,"related_course":null,"time_limit":30,"deadline":"2026-11-17T23:02:46Z","status":"draft","is_active":false,"published_at":null,"closed_at":null,"created_at":"2026-11-10T23:02:46.989308Z","updated_at":"2026-11-10T23:02:46.989314Z","questions":[],"course_title":null,"event_title":null,"total_points":0,"question_count":0,"content_editable":true},"message":"Draft quiz created."}
```

Create question (201):

```json
{"success":true,"data":{"id":1,"quiz":1,"type":"mcq","text":"Question","points":2,"order":1,"options":[{"id":1,"text":"A","is_correct":false,"order":1},{"id":2,"text":"B","is_correct":true,"order":2},{"id":3,"text":"C","is_correct":false,"order":3},{"id":4,"text":"D","is_correct":false,"order":4}]},"message":"Question created."}
```

Admin list item (`GET /quiz/quizzes/?status=draft&page_size=1`):

```json
{"success":true,"data":{"count":1,"next":null,"previous":null,"results":[{"id":1,"title":"Arabic week 1","type":"arabic","description":"Weekly assessment","related_event":null,"related_course":null,"time_limit":45,"deadline":"2026-11-17T23:02:46Z","status":"draft","is_active":false,"published_at":null,"closed_at":null,"created_at":"2026-11-10T23:02:46.989308Z","updated_at":"2026-11-10T23:02:47.049412Z","course_title":null,"event_title":null,"question_count":1,"total_points":2}]},"message":"Quizzes retrieved."}
```

Publish and close return the admin detail (`status` `published`/`closed`,
`content_editable` false). Locked field after publication:

```json
{"success":false,"data":{"code":"QUIZ_IMMUTABLE","fields":{"time_limit":["This field cannot change after publication."]}},"message":"Only the title and description of a published or closed quiz can change."}
```

The OpenAPI document (`manage.py spectacular`) carries enveloped examples for
these operations; `quiz.tests.test_authoring.SchemaWireContractTests`
validates both the real responses and the examples against it.

## Student-visible changes

- `GET /quiz/quizzes/` is now D0-paginated for students too (lead decision);
  items add `total_points`, `published_at`, `closed_at`, `created_at`,
  `updated_at` (MCQ-only counts). Filtering/visibility are unchanged.
- No student path returns `is_correct` or another key; authoring routes are admin-only.

## Tests

`quiz/tests/test_authoring.py` (validation matrices, defaults, option-ID
preservation, atomic nested writes, legacy alias bypass checks, publication and
close transitions/errors, metadata correction with snapshot preservation,
content lock with legacy history, pagination/filter/ordering, permission matrix,
key leakage, schema/wire contract) and `quiz/tests/test_authoring_concurrency.py`
(publish vs question write in both lock orders, and an API race). PostgreSQL only.
