from django.test import TestCase

from accounts.models import CustomUser
from dawah.models import Event
from quiz.models import Quiz, Question, Option, Submission, Course
from quiz.services import InvalidAnswers, QuizService


class TestQuizService(TestCase):
    """Legacy text/multiple-answer grader (not a release question type).

    Kept as legacy coverage; fixtures use only persisted enum values
    (``Question.Type``) and database-assigned primary keys so they run the same
    on PostgreSQL as on SQLite.
    """

    def setUp(self):
        # One legacy submission per student and quiz (ASSESS-02 constraint), so
        # each graded record belongs to its own student.
        student, student2, student3 = (
            CustomUser.objects.create_user(email=email, password=None)
            for email in ("django@email.com", "django2@email.com", "django3@email.com")
        )

        event = Event.objects.create(
            title="Django Intro",
            category="Tech",
            tags="django,web",
            speakers="John Doe",
            location="Online",
            start_time="2024-01-01T10:00:00Z",
            end_time="2024-01-01T12:00:00Z",
        )
        course = Course.objects.create(
            code="CSC101",
            title="Computer Science 101",
            department="CSC",
            level="100",
            semester="First",
        )
        quiz = self.quiz = Quiz.objects.create(
            title="Quiz 101",
            type="TEXT",
            related_course=course,
            related_event=event,
            time_limit=5,
        )
        # Question 1
        q1 = Question.objects.create(
            quiz=quiz, text="Django is a python web framework", type=Question.Type.text
        )
        Option.objects.create(question=q1, text="True", is_correct=True)
        Option.objects.create(question=q1, text="False", is_correct=False)
        # Question 2
        q2 = Question.objects.create(
            quiz=quiz, text="NodeJS is a javascript ___", type=Question.Type.text
        )
        Option.objects.create(question=q2, text="web framework", is_correct=False)
        Option.objects.create(question=q2, text="runtime environment", is_correct=True)
        # Question 3
        q3 = Question.objects.create(
            quiz=quiz, text="Choose all correct options: ", type=Question.Type.multi
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
        self.submission1 = Submission.objects.create(
            quiz=quiz,
            student=student,
            answers={
                str(q1.id): "true",
                str(q2.id): "runtime environment",
                str(q3.id): [
                    "The python interpreter is written in C",
                    "Pythagoras' grading engine would not be reliable without some tests",
                ],
            },
            score=0,
            is_legacy=True,
        )
        # Submission 2
        self.submission2 = Submission.objects.create(
            quiz=quiz,
            student=student2,
            answers={
                str(q1.id): "true",
                str(q2.id): "runtime environment",
                str(q3.id): [
                    "Pythagoras' grading engine would not be reliable without some tests",
                    "The python interpreter is written in C",
                    "GitHub is a version control system",
                ],
            },
            score=0,
            is_legacy=True,
        )
        # Submission 3
        self.submission3 = Submission.objects.create(
            quiz=quiz,
            student=student3,
            answers={
                str(q1.id): "true",
                str(q2.id): "web framework",
                str(q3.id): [
                    "The python interpreter is written in C",
                    "Pythagoras' grading engine would not be reliable without some tests",
                ],
            },
            score=0,
            is_legacy=True,
        )

    def test_all_answers_correct(self):
        self.assertEqual(QuizService.grade_submission(self.submission1), 3)

    def test_multiple_answers_not_correct(self):
        self.assertEqual(QuizService.grade_submission(self.submission2), 2)

    def test_txt_answer_not_correct(self):
        self.assertEqual(QuizService.grade_submission(self.submission3), 2)


    def test_persisted_question_types_are_valid_enum_values(self):
        stored = set(Question.objects.values_list("type", flat=True))
        self.assertTrue(stored <= set(Question.Type.values), stored)

    def test_malformed_legacy_answers_raise_invalid_answers_not_assertion(self):
        q1, q3 = self.quiz.question_set.order_by("pk")[0], self.quiz.question_set.order_by("pk")[2]
        cases = [
            ([], "non_field_errors"),
            ("true", "non_field_errors"),
            ({str(q1.id): ["true"]}, str(q1.id)),   # list for a single-answer question
            ({str(q1.id): 1}, str(q1.id)),
            ({str(q3.id): "The python interpreter is written in C"}, str(q3.id)),  # str for multi
            ({str(q3.id): ["ok", 3]}, str(q3.id)),  # non-string list element
        ]
        for answers, field in cases:
            with self.subTest(answers=answers):
                submission = Submission(quiz=self.quiz, answers=answers, score=0)
                with self.assertRaises(InvalidAnswers) as caught:
                    QuizService.grade_submission(submission)
                self.assertIn(field, caught.exception.errors)
