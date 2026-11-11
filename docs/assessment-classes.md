# Arabic classes, enrollment and modules (ASSESS-07)

Governing design: D3 "Arabic classes enrollment and modules"; frozen frontend contract in
`contracts/frontend-requests-for-backend.md` (ASSESS-07 sections). Code: `quiz/learning.py`
(services, locks), `quiz/learning_serializers.py`, `quiz/learning_views.py`,
`quiz/resources.py` (the only adapter to dawah resource visibility/payloads), migration
`quiz.0008`.

## Model

- `Course.kind`: `arabic` (release classes) or `general` (legacy/general academic). Every
  course that existed before `quiz.0008` is `general`, `draft`, with null timestamps; the class
  API never serves `general` rows (404), so legacy courses are not presented as Arabic classes.
- Class lifecycle `draft -> published -> closed` (terminal). Closed is read-only.
- `Enrollment`: one row per (course, student); `status` `active|withdrawn`, `enrolled_at`
  (latest activation), `withdrawn_at` (set exactly while withdrawn). Re-enrollment reactivates
  the same row. Legacy rows: `active`, null timestamps. Course/student FKs are PROTECT.
- `LearningModule`: class, title, plain-text body, `order` (unique per class, deferred
  constraint), `draft|published`. `ModuleResource`: ordered attachments to `dawah.EventResource`,
  resource FK PROTECT (an attached resource or its event cannot be deleted).
- Linked quizzes come from `Quiz.related_course`. A newly set `related_course` must be a
  draft/published Arabic class; it never changes quiz eligibility.

## Endpoints

All responses use the D0 envelope; lists are D0-paginated (`page`, `page_size` <= 100).

| Route | Method | Who | Behavior |
|---|---|---|---|
| `/quiz/classes/` | GET | active user | Admin: all Arabic classes, `-id`, `status` filter. Student: published catalog, `code, id` |
| `/quiz/classes/` | POST | admin | `{code, title, description}` -> 201 draft |
| `/quiz/classes/{id}/` | GET | active user | Admin: any; student: published/closed (draft 404) |
| `/quiz/classes/{id}/` | PATCH | admin | partial `{code,title,description}`; closed 409 `CLASS_CLOSED` |
| `/quiz/classes/{id}/` | DELETE | admin | draft only (else 409 `INVALID_TRANSITION`); enrollments/quizzes 409 `RESOURCE_REFERENCED`; cascades its modules |
| `/quiz/classes/{id}/publish/`, `/close/` | POST | admin | no body; repeat is 200 no-op; publish closed / close draft 409 `INVALID_TRANSITION` |
| `/quiz/classes/{id}/enrollment/` | GET | student | own record or 404 |
| `/quiz/classes/{id}/enrollment/` | POST | student | `{}`; 201 new, 200 same/reactivated record; closed 409 `CLASS_CLOSED`; draft 404 |
| `/quiz/classes/{id}/enrollment/` | DELETE | student | 200 withdrawn record (idempotent); never enrolled 404; closed (active) 409 |
| `/quiz/classes/{id}/enrollments/` | GET | admin | roster; `search` (first/last/full name, email), `status` |
| `/quiz/classes/{id}/enrollments/` | POST | admin | `{student_id, status?: active|withdrawn}`; 201 created else 200; draft 409 `CLASS_NOT_PUBLISHED`; closed 409 |
| `/quiz/classes/{id}/modules/` | GET | active user | Admin: all (`status` filter). Student: published, gated (below) |
| `/quiz/classes/{id}/modules/` | POST | admin | `{title, body, order?, resource_ids?}` -> 201 draft |
| `/quiz/classes/{id}/modules/reorder/` | POST | admin | `{module_ids:[every id]}` -> 200 ordered list; mismatch 400 |
| `/quiz/classes/{id}/modules/{mid}/` | GET | active user | Admin: any; student: gated |
| `/quiz/classes/{id}/modules/{mid}/` | PATCH | admin | partial; `resource_ids` replaces the set |
| `/quiz/classes/{id}/modules/{mid}/` | DELETE | admin | draft only (409 `UNPUBLISH_REQUIRED`) |
| `/quiz/classes/{id}/modules/{mid}/publish/`, `/unpublish/` | POST | admin | idempotent; closed class 409 |

Student module gating: published class + active enrollment -> 200; published class without an
active enrollment (never enrolled, withdrawn, enrolled elsewhere) -> 403 `ENROLLMENT_REQUIRED`;
draft/closed/legacy/missing class, or draft/foreign/missing module -> 404. Admins preview drafts.

Attachments: newly added `resource_ids` must be visible (`EventResource.objects.visible()`:
published resource and event, ready asset or valid YouTube ID), otherwise 400
`resource_ids: ["Resource N is not published or not available."]`; ids already attached are
accepted even if hidden. `resources[]` (admin and student) holds only currently visible
attachments, each exactly the anonymous `GET /dawah/resources/{id}/` payload; admins also get
`resource_ids` (all, in order) and `hidden_resource_ids`. `quizzes[]` is `{id,title,status}`
(students: published/closed only). No answer keys anywhere.

Error codes: `CLASS_CLOSED`, `CLASS_NOT_PUBLISHED`, `ENROLLMENT_REQUIRED` (403),
`UNPUBLISH_REQUIRED`, `INVALID_TRANSITION`, `RESOURCE_REFERENCED`, `VALIDATION_ERROR` (flat
dotted `fields`, e.g. `resource_ids.0`).

## Examples

Student module detail:

```json
{"success":true,"data":{"id":1,"title":"Arabic greetings","body":"Lesson text","order":1,"resources":[{"id":2,"event_id":1,"title":"Greetings video","type":"vid","status":"published","storage_status":"not_applicable","duration":null,"media_url":null,"playback":{"provider":"youtube","video_id":"abcdefghijk","watch_url":"https://www.youtube.com/watch?v=abcdefghijk","embed_url":"https://www.youtube-nocookie.com/embed/abcdefghijk"},"content_type":null,"size_bytes":null,"uploaded_at":null,"preview_url":null,"published_at":null,"created_at":"2026-11-11T00:27:39.767135Z","updated_at":"2026-11-11T00:27:39.767139Z","created_by":null,"legacy_file_present":null,"event":{"id":1,"title":"Arabic circle","speakers":"Ustadh Musa","category":"special_lecture","start_time":"2026-09-28T17:00:00Z","summary":"Greetings"}}],"quizzes":[{"id":1,"title":"Arabic week 1","status":"published"}]},"message":"Module retrieved."}
```

Enrollment: `{"success":true,"data":{"id":1,"course_id":1,"student_id":2,"status":"active","enrolled_at":"2026-11-11T00:27:39.886733Z","withdrawn_at":null},"message":"Enrolled."}`.
Roster row adds `student_name`, `student_email`, `updated_at`. Class payload: `id, code, title,
description, kind, status, published_at, closed_at, created_at, updated_at, module_count`
(+ admin `enrollment_count` = active enrollments; student `module_count` = published modules).

## Concurrency

Class lifecycle/edits and all module writes lock the class row `FOR UPDATE` (order checks,
append and reorder are serialized; the deferred unique constraint is the backstop). Enrollment
writes take the class `FOR SHARE` and the enrollment row `FOR UPDATE`; a racing insert loses
the unique constraint and reuses the winner's row. New attachments take `FOR KEY SHARE` on the
resource rows so a concurrent resource deletion meets the PROTECT link.

## Migration and limits

- `quiz.0008` is additive; rollback drops the new columns/tables (module/attachment data and
  enrollment status/timestamps are lost; course and enrollment rows survive).
- `manage.py assessment_preflight` lists legacy course IDs and enrollment counts carried
  forward. Memberships lost by `quiz.0002` are not recreated; importing a legacy course as an
  Arabic class needs an explicit lead decision.
- Deleting an attached (unpublished) resource through `DELETE /dawah/events/{e}/resources/{id}/`
  is refused by PROTECT, but dawah's `delete_resource` does not yet map `ProtectedError`, so it
  surfaces as a sanitized 500 (event deletion already maps to 409 `RESOURCE_REFERENCED`).
- No throttle is applied: D5 lists none for these routes.
