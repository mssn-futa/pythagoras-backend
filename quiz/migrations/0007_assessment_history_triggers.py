"""ASSESS-02 database guards (PostgreSQL triggers) for content and history.

Every path, including bulk ORM calls and raw SQL, is refused with SQLSTATE
``23Q01`` (immutable; Django raises IntegrityError, the API maps it to 409
``QUIZ_IMMUTABLE``) or ``23Q02`` (submission/attempt mismatch):

* quiz_question / quiz_option INSERT, UPDATE, DELETE when the (old or new)
  parent quiz is not a draft or has any attempt or submission (legacy
  included): content-locked quizzes are replaced, not edited.
* quiz_quiz UPDATE of a non-draft quiz: type, time_limit, deadline,
  related_event_id, related_course_id, published_at are frozen; status may
  only go published -> closed, and closed_at only changes with it. DELETE of a
  non-draft quiz is refused.
* quiz_attempt UPDATE of quiz_id, student_id, started_at, expires_at, or of
  anything once the attempt is submitted/expired; DELETE is refused.
* quiz_submission UPDATE/DELETE are refused (insert-only); INSERT must match
  its attempt's quiz/student and may not create ``is_legacy`` rows (every
  legacy row predates this migration).

TRUNCATE (test flushes, operator restores) does not fire row triggers.
Rollback: the reverse drops the triggers, but first refuses (like 0006) while
attempts or release submissions exist.
"""
from django.db import migrations

FORWARD = r"""
CREATE FUNCTION quiz_content_locked(qid bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT qid IS NOT NULL AND (
        EXISTS (SELECT 1 FROM quiz_quiz WHERE id = qid AND status <> 'draft')
        OR EXISTS (SELECT 1 FROM quiz_attempt WHERE quiz_id = qid)
        OR EXISTS (SELECT 1 FROM quiz_submission WHERE quiz_id = qid));
$$;

CREATE FUNCTION quiz_refuse(message text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '%', message USING ERRCODE = '23Q01';
END $$;

CREATE FUNCTION quiz_question_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND quiz_content_locked(OLD.quiz_id) THEN
        PERFORM quiz_refuse(format('quiz %s content is locked', OLD.quiz_id));
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') AND quiz_content_locked(NEW.quiz_id) THEN
        PERFORM quiz_refuse(format('quiz %s content is locked', NEW.quiz_id));
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER quiz_question_content_guard
BEFORE INSERT OR UPDATE OR DELETE ON quiz_question
FOR EACH ROW EXECUTE FUNCTION quiz_question_guard();

CREATE FUNCTION quiz_option_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') AND quiz_content_locked(
            (SELECT quiz_id FROM quiz_question WHERE id = OLD.question_id)) THEN
        PERFORM quiz_refuse(format('option %s belongs to a locked quiz', OLD.id));
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') AND quiz_content_locked(
            (SELECT quiz_id FROM quiz_question WHERE id = NEW.question_id)) THEN
        PERFORM quiz_refuse(format('question %s belongs to a locked quiz', NEW.question_id));
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER quiz_option_content_guard
BEFORE INSERT OR UPDATE OR DELETE ON quiz_option
FOR EACH ROW EXECUTE FUNCTION quiz_option_guard();

CREATE FUNCTION quiz_quiz_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    closing boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            PERFORM quiz_refuse(format('quiz %s is not a draft and cannot be deleted', OLD.id));
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> 'draft' THEN
        closing := OLD.status = 'published' AND NEW.status = 'closed';
        IF NEW.type IS DISTINCT FROM OLD.type
                OR NEW.time_limit IS DISTINCT FROM OLD.time_limit
                OR NEW.deadline IS DISTINCT FROM OLD.deadline
                OR NEW.related_event_id IS DISTINCT FROM OLD.related_event_id
                OR NEW.related_course_id IS DISTINCT FROM OLD.related_course_id
                OR NEW.published_at IS DISTINCT FROM OLD.published_at THEN
            PERFORM quiz_refuse(format('quiz %s grading/timing fields are frozen', OLD.id));
        END IF;
        IF (NEW.status IS DISTINCT FROM OLD.status
                OR NEW.closed_at IS DISTINCT FROM OLD.closed_at) AND NOT closing THEN
            PERFORM quiz_refuse(format('quiz %s cannot go from %s to %s',
                                       OLD.id, OLD.status, NEW.status));
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER quiz_quiz_lifecycle_guard
BEFORE UPDATE OR DELETE ON quiz_quiz
FOR EACH ROW EXECUTE FUNCTION quiz_quiz_guard();

CREATE FUNCTION quiz_attempt_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM quiz_refuse(format('attempt %s is history and cannot be deleted', OLD.id));
    END IF;
    IF NEW.quiz_id IS DISTINCT FROM OLD.quiz_id
            OR NEW.student_id IS DISTINCT FROM OLD.student_id
            OR NEW.started_at IS DISTINCT FROM OLD.started_at
            OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        PERFORM quiz_refuse(format('attempt %s identity and timing are fixed', OLD.id));
    END IF;
    IF OLD.status <> 'in_progress' AND (NEW.status IS DISTINCT FROM OLD.status
            OR NEW.finalized_at IS DISTINCT FROM OLD.finalized_at) THEN
        PERFORM quiz_refuse(format('attempt %s is %s and cannot change', OLD.id, OLD.status));
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER quiz_attempt_history_guard
BEFORE UPDATE OR DELETE ON quiz_attempt
FOR EACH ROW EXECUTE FUNCTION quiz_attempt_guard();

CREATE FUNCTION quiz_submission_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        PERFORM quiz_refuse(format('submission %s is final', OLD.id));
    END IF;
    IF NEW.is_legacy THEN
        PERFORM quiz_refuse('legacy submissions are migrated, never created');
    END IF;
    IF NEW.attempt_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM quiz_attempt
            WHERE id = NEW.attempt_id AND quiz_id = NEW.quiz_id
              AND student_id = NEW.student_id) THEN
        RAISE EXCEPTION 'submission quiz/student must match attempt %', NEW.attempt_id
            USING ERRCODE = '23Q02';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER quiz_submission_history_guard
BEFORE INSERT OR UPDATE OR DELETE ON quiz_submission
FOR EACH ROW EXECUTE FUNCTION quiz_submission_guard();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS quiz_submission_history_guard ON quiz_submission;
DROP TRIGGER IF EXISTS quiz_attempt_history_guard ON quiz_attempt;
DROP TRIGGER IF EXISTS quiz_quiz_lifecycle_guard ON quiz_quiz;
DROP TRIGGER IF EXISTS quiz_option_content_guard ON quiz_option;
DROP TRIGGER IF EXISTS quiz_question_content_guard ON quiz_question;
DROP FUNCTION IF EXISTS quiz_submission_guard();
DROP FUNCTION IF EXISTS quiz_attempt_guard();
DROP FUNCTION IF EXISTS quiz_quiz_guard();
DROP FUNCTION IF EXISTS quiz_option_guard();
DROP FUNCTION IF EXISTS quiz_question_guard();
DROP FUNCTION IF EXISTS quiz_refuse(text);
DROP FUNCTION IF EXISTS quiz_content_locked(bigint);
"""


class HistoryRollbackError(RuntimeError):
    pass


def install(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(FORWARD, params=None)


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(REVERSE, params=None)


def refuse_rollback_with_release_history(apps, schema_editor):
    db = schema_editor.connection.alias
    attempts = apps.get_model('quiz', 'Attempt').objects.using(db).count()
    release = apps.get_model('quiz', 'Submission').objects.using(db).filter(
        is_legacy=False).count()
    if attempts or release:
        raise HistoryRollbackError(
            f'Refusing to unapply quiz.0007: {attempts} attempts and {release} release '
            'submissions would lose their database guards and, further back, their history. '
            'Restore a backup taken before the upgrade instead.')


class Migration(migrations.Migration):

    dependencies = [
        ('quiz', '0006_assessment_lifecycle_constraints'),
    ]

    operations = [
        migrations.RunPython(install, uninstall, elidable=False),
        # Last, so its reverse runs first and refuses before the triggers go.
        migrations.RunPython(migrations.RunPython.noop, refuse_rollback_with_release_history,
                             elidable=False),
    ]
