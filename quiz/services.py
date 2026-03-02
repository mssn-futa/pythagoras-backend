import re
from django.db.models import Prefetch
from .models import Option, Submission


class QuizService:

    @classmethod
    def grade_submission(cls, submission: Submission):
        quiz = submission.quiz

        questions = quiz.question_set.prefetch_related(
            Prefetch(
                "option_set",
                queryset=Option.objects.filter(is_correct=True),
                to_attr="correct_options",
            )
        )

        total_score = 0
        for question in questions:
            answer = submission.answers.get(str(question.id), None)
            if answer is None:
                continue

            # multi -> a question with multiple answers
            if question.type in ("multi", "MULTIPLE_ANSWER"):
                assert isinstance(answer, list)
            else:
                assert isinstance(answer, str)
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

        return total_score

    @staticmethod
    def _normalize_string(string: str):
        """
        Normalize a string by stripping leading/trailing whitespace, replacing multiple spaces with a single space,
        and converting to lowercase.
        """
        stripped = string.strip()
        return re.sub(r"\s+", " ", stripped).lower()
