from django.test import TestCase

from accounts.models import CustomUser
from quiz.models import Quiz, Question, Option, Submission
from quiz.services import QuizService


# Create your tests here.
class TestQuizService(TestCase):
    def setUp(self):
        student = CustomUser.objects.create(
            email="django@email.com", password="DjangoBoy!!"
        )
        quiz = Quiz.objects.create(title="Quiz 101", type="TXT")
        # Question 1
        q1 = Question.objects.create(
            pk=1, quiz=quiz, text="Django is a python web framework", type="TXT"
        )
        Option.objects.create(question=q1, text="True", is_correct=True)
        Option.objects.create(question=q1, text="False", is_correct=False)
        # Question 2
        q2 = Question.objects.create(
            pk=2, quiz=quiz, text="NodeJS is a javascript ___", type="TXT"
        )
        Option.objects.create(question=q2, text="web framework", is_correct=False)
        Option.objects.create(question=q2, text="runtime environment", is_correct=True)
        # Question 3
        q3 = Question.objects.create(
            pk=3, quiz=quiz, text="Choose all correct options: ", type="MA"
        )
        Option.objects.create(
            question=q3, text="The python interpreter is written in C", is_correct=True
        )
        Option.objects.create(
            question=q3, text="GitHub is a version control system", is_correct=False
        )
        Option.objects.create(
            question=q3,
            text="Pythagoras' grading engine would not be reliable without some tests",
            is_correct=True,
        )
        # Submission 1
        Submission.objects.create(
            pk=1,
            quiz=quiz,
            student=student,
            answers={
                "1": "true",
                "2": "runtime environment",
                "3": [
                    "The python interpreter is written in C",
                    "Pythagoras' grading engine would not be reliable without some tests",
                ],
            },
        )
        # Submission 2
        Submission.objects.create(
            pk=2,
            quiz=quiz,
            student=student,
            answers={
                "1": "true",
                "2": "runtime environment",
                "3": [
                    "Pythagoras' grading engine would not be reliable without some tests"
                    "The python interpreter is written in C",
                    "GitHub is a version control system",
                ],
            },
        )
        # Submission 3
        Submission.objects.create(
            pk=3,
            quiz=quiz,
            student=student,
            answers={
                "1": "true",
                "2": "web framework",
                "3": [
                    "The python interpreter is written in C",
                    "Pythagoras' grading engine would not be reliable without some tests",
                ],
            },
        )

    def test_all_answers_correct(self):
        self.assertEqual(QuizService.grade_submission(Submission.objects.get(pk=1)), 3)

    def test_multiple_answers_not_correct(self):
        self.assertEqual(QuizService.grade_submission(Submission.objects.get(pk=2)), 2)

    def test_txt_answer_not_correct(self):
        self.assertEqual(QuizService.grade_submission(Submission.objects.get(pk=3)), 2)
