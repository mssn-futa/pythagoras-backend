"""Assessment domain errors (no HTTP imports).

Each state error carries the D0 domain ``code`` its HTTP adapter should use
(409 unless noted). Views translate them with ``quiz.views.domain_errors``;
ASSESS-03/04 should do the same rather than re-deriving codes.
"""


class AssessmentError(Exception):
    """Base for assessment state errors. ``code``/``message`` map to a D0 error."""

    code = 'CONFLICT'
    status_code = 409
    default_message = 'This assessment operation is not allowed.'

    def __init__(self, message=None, *, fields=None):
        self.message = message or self.default_message
        self.fields = fields
        super().__init__(self.message)


class QuizImmutable(AssessmentError):
    """A published/closed quiz's grading/timing content or a finalized record would change."""

    code = 'QUIZ_IMMUTABLE'
    default_message = 'Published assessment content cannot be changed.'


class InvalidTransition(AssessmentError):
    """A lifecycle transition D3 does not allow (e.g. closed -> published)."""

    code = 'INVALID_TRANSITION'
    default_message = 'This lifecycle transition is not allowed.'


class QuizReferenced(AssessmentError):
    """A quiz with attempts/submissions cannot be deleted (history is protected)."""

    code = 'RESOURCE_REFERENCED'
    default_message = 'This quiz has attempts or submissions and cannot be deleted.'


class QuizUnavailable(AssessmentError):
    """No new attempt: the quiz is not published or its deadline has passed."""

    code = 'QUIZ_UNAVAILABLE'
    default_message = 'This quiz is not available.'


class AttemptExpired(AssessmentError):
    """First finalization at/after the attempt's server expiry (A10)."""

    code = 'ATTEMPT_EXPIRED'
    default_message = 'This attempt has expired.'


class SubmissionConflict(AssessmentError):
    """A finalized submission exists with different answers (never overwritten)."""

    code = 'SUBMISSION_CONFLICT'
    default_message = 'Different answers were already submitted for this attempt.'


class SubmissionImmutable(AssessmentError):
    """Finalized submissions (and their snapshots) are never updated or deleted."""

    code = 'QUIZ_IMMUTABLE'
    default_message = 'Finalized submissions cannot be changed or deleted.'


class AuthoringInvalid(AssessmentError):
    """Authoring input breaks a rule checked under the quiz lock (400 VALIDATION_ERROR).

    ``fields`` maps dotted field paths (``order``, ``options.2.text``) to message lists.
    """

    code = 'VALIDATION_ERROR'
    default_message = 'Correct the highlighted fields.'


class PublicationInvalid(AuthoringInvalid):
    """The quiz graph fails publication validation (400 VALIDATION_ERROR with ``fields``)."""

    default_message = 'This quiz cannot be published.'


class ClassClosed(AssessmentError):
    """A closed Arabic class is read-only: no edits, modules or roster changes."""

    code = 'CLASS_CLOSED'
    default_message = 'This class is closed and can no longer be changed.'


class ClassNotPublished(AssessmentError):
    """Enrollment needs a published class."""

    code = 'CLASS_NOT_PUBLISHED'
    default_message = 'This class is not open for enrollment yet.'


class UnpublishRequired(AssessmentError):
    """A published module must be unpublished before it is deleted."""

    code = 'UNPUBLISH_REQUIRED'
    default_message = 'Unpublish this module before deleting it.'


class ClassReferenced(AssessmentError):
    """A class with enrollments or quizzes cannot be deleted (history is protected)."""

    code = 'RESOURCE_REFERENCED'
    default_message = ('This class has enrollments or linked quizzes and cannot be deleted; '
                       'close it instead.')


class EnrollmentRequired(AssessmentError):
    """Module access needs an active enrollment in the published class (403)."""

    code = 'ENROLLMENT_REQUIRED'
    status_code = 403
    default_message = 'An active enrollment in this class is required to read its modules.'


class InvalidAnswers(ValueError):
    """Answers do not have the structure a grader requires.

    ``errors`` maps a question-ID string (or ``non_field_errors``) to a list of
    messages. ``as_fields()`` nests them under ``field`` (``answers`` by
    default) for a D0 ``VALIDATION_ERROR`` ``fields`` object; ``field=None``
    means ``errors`` already names request fields.
    """

    def __init__(self, errors, field='answers'):
        self.errors = errors
        self.field = field
        super().__init__('Invalid answers.')

    def as_fields(self):
        return self.errors if self.field is None else {self.field: self.errors}


# SQLSTATEs raised by the quiz.0007 triggers (class 23, so Django raises
# IntegrityError). IMMUTABLE: frozen content/state/history; MISMATCH: a
# submission whose quiz/student differ from its attempt's.
IMMUTABLE_SQLSTATE = '23Q01'
MISMATCH_SQLSTATE = '23Q02'


def db_error_sqlstate(exc):
    """SQLSTATE of a wrapped database error (psycopg2 ``pgcode`` or psycopg 3
    ``sqlstate``; ``None`` if unavailable)."""
    cause = getattr(exc, '__cause__', None)
    return getattr(cause, 'pgcode', None) or getattr(cause, 'sqlstate', None)


def is_immutable_db_error(exc):
    """True for a trigger refusal that should surface as ``QuizImmutable`` (409)."""
    return db_error_sqlstate(exc) == IMMUTABLE_SQLSTATE
