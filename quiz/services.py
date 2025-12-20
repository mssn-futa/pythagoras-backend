import re
from django.db.models import Prefetch
from .models import Option, Submission


class QuizService:

    @classmethod
    def grade_submission(cls, submission: Submission):
        quiz = submission.quiz

        correct_options = Option.objects.filter(is_correct=True)
        questions = quiz.questions.prefetch_related(
            Prefetch("options", queryset=correct_options, to_attr="correct_options")
        )

        total_score = 0
        # import pdb; pdb.set_trace()
        for question in questions:
            answer = submission.answers.get(str(question.id), None)
            if answer is None:
                continue
            # MA -> a question with multiple answers
            if question.type == "MA":
                assert type(answer) == list
            else:
                assert type(answer) == str
                answer = [answer]

            answer_set = set(map(cls._normalize_string, answer))
            correct_option_set = set(
                map(
                    cls._normalize_string,
                    [opt.text for opt in question.correct_options],
                )
            )
            if answer_set == correct_option_set:
                total_score += question.points

        return total_score

    @staticmethod
    def _normalize_string(string: str):
        # remove trailing and leading whitespaces
        stripped = string.strip()
        # replace multiple whitespaces between words
        return re.sub(r"\s+", " ", stripped).lower()
