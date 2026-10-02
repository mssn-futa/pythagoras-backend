from django.conf import settings
from django.db import models
from dawah.models import Event
from accounts.models import CustomUser


class Course (models.Model):
    code = models.CharField(max_length=7)
    title = models.CharField(max_length=100)
    department = models.CharField(max_length=100)
    level = models.CharField(max_length=3, choices=CustomUser.LEVEL_CHOICES)
    semester = models.CharField(max_length=10)

    students = models.ManyToManyField(
        settings.AUTH_USER_MODEL, related_name='courses'
    )

    def __str__(self):
        return self.title


class Quiz(models.Model):
    title = models.CharField(max_length=250)
    type = models.CharField(max_length=250)
    description = models.CharField(max_length=250)

    related_event = models.ForeignKey(Event, on_delete=models.CASCADE)
    related_course = models.ForeignKey(
        Course, on_delete=models.CASCADE)

    time_limit = models.PositiveSmallIntegerField(
        help_text="Time limit in minutes")
    is_active = models.BooleanField(default=False)

    def __str__(self):
        return self.title


class Submission(models.Model):
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE)
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    answers = models.JSONField()
    score = models.PositiveSmallIntegerField()
    submitted_at = models.DateTimeField(auto_now_add=True)
    graded_at = models.DateTimeField(blank=True, null=True)


class Question(models.Model):
    class Type(models.TextChoices):
        mcq = 'mcq', 'MULTIPLE_CHOICE'
        multi = 'multi', 'MULTIPLE_ANSWER'
        text = 'text', 'TEXT'

    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE)
    type = models.CharField(max_length=5, choices=Type)
    text = models.TextField()
    points = models.PositiveSmallIntegerField(default=1)

    def __str__(self):
        return self.text


class Option(models.Model):
    question = models.ForeignKey(Question, on_delete=models.CASCADE)

    text = models.CharField(max_length=250)
    is_correct = models.BooleanField(default=False)

    def __str__(self):
        return self.text
