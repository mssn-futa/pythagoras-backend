"""Assessment grading and answer validation.

Release behaviour (D3/A08): MCQ only. A submission's ``answers`` is a JSON
object whose keys are canonical decimal question-ID strings of the quiz and
whose values are integer option IDs of that question. Grading happens only on
the server against the persisted correct option and question points.

Finalization (locking, timing, replay) lives in ``quiz.lifecycle``; the
ASSESS-01/02 direct submission (``QuizService.submit``, which created and
finalized an attempt in one step) was retired by ASSESS-04 so no path bypasses
the timed attempt.

``QuizService.grade_submission`` is the historical text/multi-answer grader.
It is retained only as legacy coverage for records stored in that shape and is
not used by any release endpoint.
"""
import re
from dataclasses import dataclass

from django.db.models import Prefetch

from .errors import AttemptExpired, InvalidAnswers, SubmissionConflict  # noqa: F401 (re-exported)
from .models import MAX_STORED_POINTS, Option, Question, Submission

# Capacity of Submission.score/total_points (widened to integer by ASSESS-02).
MAX_STORED_SCORE = MAX_STORED_POINTS

_CANONICAL_ID = re.compile(r'[1-9][0-9]*')
SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class GradingResult:
    score: int
    total_points: int
    snapshot: dict


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
    def grade(quiz, answers):
        """Grade answers from ``validate_answers``; return a ``GradingResult``.

        Weighted by persisted question points, against persisted correct
        option IDs, MCQ questions only, in persisted (order, id) order.
        ``snapshot`` (version 1) is what a finalized submission stores::

            {"version": 1, "quiz_id": 45, "quiz_title": "Arabic week 1",
             "score": 2, "total_points": 5,
             "questions": [{"question_id": 10, "order": 1, "points": 2,
                            "selected_option_id": 102, "awarded_points": 2}, ...]}

        It deliberately holds no correct-option IDs. Unanswered questions have
        ``selected_option_id: null`` and score zero.
        """
        questions = quiz.question_set.filter(type=Question.Type.mcq).order_by(
            'order', 'pk').prefetch_related(
            Prefetch('option_set', queryset=Option.objects.filter(is_correct=True),
                     to_attr='correct_options'))
        rows = []
        score = total = 0
        for question in questions:
            selected = answers.get(str(question.pk))
            correct = {option.pk for option in question.correct_options}
            # Exactly one correct option is the release rule (A08); a legacy
            # question with zero or several correct options never awards points.
            awarded = question.points if selected is not None and correct == {selected} else 0
            score += awarded
            total += question.points
            rows.append({'question_id': question.pk, 'order': question.order,
                         'points': question.points, 'selected_option_id': selected,
                         'awarded_points': awarded})
        snapshot = {'version': SNAPSHOT_VERSION, 'quiz_id': quiz.pk, 'quiz_title': quiz.title,
                    'score': score, 'total_points': total, 'questions': rows}
        return GradingResult(score=score, total_points=total, snapshot=snapshot)

    @classmethod
    def grade_answers(cls, quiz, answers):
        """Weighted score for answers already returned by ``validate_answers``."""
        return cls.grade(quiz, answers).score

    @staticmethod
    def available_points(quiz):
        return sum(quiz.question_set.filter(type=Question.Type.mcq).values_list('points', flat=True))

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
