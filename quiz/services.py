"""Assessment grading and answer validation.

Release behaviour (D3/A08): MCQ only. A submission's ``answers`` is a JSON
object whose keys are canonical decimal question-ID strings of the quiz and
whose values are integer option IDs of that question. Grading happens only on
the server against the persisted correct option and question points.

``QuizService.grade_submission`` is the historical text/multi-answer grader.
It is retained only as legacy coverage for records stored in that shape and is
not used by any release endpoint.
"""
import json
import re

from django.db import transaction
from django.db.models import Prefetch

from .models import Option, Question, Quiz, Submission

# Submission.score is a PositiveSmallIntegerField until ASSESS-02 widens it.
MAX_STORED_SCORE = 32767

_CANONICAL_ID = re.compile(r'[1-9][0-9]*')


class SubmissionConflict(Exception):
    """The student already has a submission with different answers (409)."""


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


def _canonical_json(value):
    # Strict comparison: JSON true/1/1.0 are different answers.
    return json.dumps(value, sort_keys=True)


def is_canonical_id(key):
    """True for a decimal ID string without sign, padding, spaces or aliases."""
    return isinstance(key, str) and _CANONICAL_ID.fullmatch(key) is not None


class QuizService:

    @staticmethod
    def validate_answers(quiz, answers):
        """Validate release MCQ answers for ``quiz``; return ``{str(qid): option_id}``.

        Raises ``InvalidAnswers`` for: a non-object, non-canonical/unknown or
        foreign question keys, non-integer values (bools, strings, lists,
        objects, floats), options that do not belong to the question, and
        answers to legacy non-MCQ questions. Missing questions are allowed and
        score zero.
        """
        if not isinstance(answers, dict):
            raise InvalidAnswers({'non_field_errors': ['Answers must be a JSON object.']})

        questions = {
            str(question.pk): question
            for question in quiz.question_set.prefetch_related('option_set')
        }
        errors = {}
        normalized = {}
        for key, value in answers.items():
            question = questions.get(key) if is_canonical_id(key) else None
            if question is None:
                errors[str(key)] = ['Not a question of this quiz.']
                continue
            if question.type != Question.Type.mcq:
                errors[key] = ['This question type cannot be answered.']
                continue
            if type(value) is not int:
                errors[key] = ['Answer must be the integer ID of one option.']
                continue
            if value not in {option.pk for option in question.option_set.all()}:
                errors[key] = ['Not an option of this question.']
                continue
            normalized[key] = value
        if errors:
            raise InvalidAnswers(errors)
        return normalized

    @staticmethod
    def grade_answers(quiz, answers):
        """Weighted score for answers already returned by ``validate_answers``."""
        score = 0
        questions = quiz.question_set.filter(type=Question.Type.mcq).prefetch_related(
            Prefetch('option_set', queryset=Option.objects.filter(is_correct=True),
                     to_attr='correct_options')
        )
        for question in questions:
            selected = answers.get(str(question.pk))
            correct = {option.pk for option in question.correct_options}
            # Exactly one correct option is the release rule (A08); a legacy
            # question with zero or several correct options never awards points.
            if selected is not None and correct == {selected}:
                score += question.points
        return score

    @staticmethod
    def available_points(quiz):
        return sum(quiz.question_set.filter(type=Question.Type.mcq).values_list('points', flat=True))

    @classmethod
    def submit(cls, *, quiz_id, student, answers):
        """Grade and persist a legacy direct submission; return ``(submission, created)``.

        Temporary until the ASSESS-04 attempt contract. Under the quiz row lock:
        an existing submission by this student is returned unchanged for an
        identical answer map (replay, also after the quiz is deactivated) or is a
        ``SubmissionConflict``; otherwise the quiz must be active, answers
        are validated, and score/graded_at are computed here, never taken from
        the client. Raises ``InvalidAnswers`` (400) or ``SubmissionConflict`` (409).
        Domain exceptions keep this module free of HTTP imports.
        """
        with transaction.atomic():
            quiz = Quiz.objects.select_for_update().filter(pk=quiz_id).first()
            existing = None
            if quiz is not None:
                existing = Submission.objects.filter(quiz=quiz, student=student).order_by('pk').first()
            if existing is not None:
                if _canonical_json(existing.answers) == _canonical_json(answers):
                    return existing, False
                raise SubmissionConflict()
            if quiz is None or not quiz.is_active:
                raise InvalidAnswers({'quiz': ['This quiz is not available.']}, field=None)
            normalized = cls.validate_answers(quiz, answers)
            if cls.available_points(quiz) > MAX_STORED_SCORE:
                raise InvalidAnswers({'quiz': ['This quiz cannot be graded until its points '
                                               'are reduced.']}, field=None)
            submission = Submission.objects.create(
                quiz=quiz,
                student=student,
                answers=normalized,
                score=cls.grade_answers(quiz, normalized),
            )
            # Graded synchronously: one instant for both timestamps
            # (submitted_at is auto_now_add, so it is set by the insert).
            submission.graded_at = submission.submitted_at
            submission.save(update_fields=['graded_at'])
        return submission, True

    # ------------------------------------------------------------------
    # Legacy grader (text / multiple-answer, option text comparison).
    # ------------------------------------------------------------------

    @classmethod
    def grade_submission(cls, submission: Submission):
        quiz = submission.quiz
        answers_by_question = submission.answers
        if not isinstance(answers_by_question, dict):
            raise InvalidAnswers({'non_field_errors': ['Answers must be a JSON object.']})

        questions = quiz.question_set.prefetch_related(
            Prefetch(
                "option_set",
                queryset=Option.objects.filter(is_correct=True),
                to_attr="correct_options",
            )
        )

        total_score = 0
        errors = {}
        for question in questions:
            key = str(question.id)
            answer = answers_by_question.get(key, None)
            if answer is None:
                continue

            # multi -> a question with multiple answers
            if question.type == Question.Type.multi:
                if not isinstance(answer, list) or not all(isinstance(item, str) for item in answer):
                    errors[key] = ['Answer must be a list of option texts.']
                    continue
            else:
                if not isinstance(answer, str):
                    errors[key] = ['Answer must be an option text.']
                    continue
                answer = [answer]

            answers = set(map(cls._normalize_string, answer))
            correct_options = set(
                map(
                    cls._normalize_string,
                    [option.text for option in question.correct_options],
                )
            )
            if answers == correct_options:
                total_score += question.points

        if errors:
            raise InvalidAnswers(errors)
        return total_score

    @staticmethod
    def _normalize_string(string: str):
        """
        Normalize a string by stripping leading/trailing whitespace, replacing multiple spaces with a single space,
        and converting to lowercase.
        """
        stripped = string.strip()
        return re.sub(r"\s+", " ", stripped).lower()
