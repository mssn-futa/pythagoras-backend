from django.conf import settings
from django.db import models

# Create your models here.
class Quiz(models.Model):
    title = models.CharField(max_length=256)
    type = models.CharField(max_length=256)
    description = models.TextField(blank=True, null=True)
    time_limit = models.PositiveSmallIntegerField(default=0)  # in mins
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.title
    
class Question(models.Model):
    class Types(models.TextChoices):
        MULTIPLE_CHOICE = 'MCQ', 'Multiple Choice'
        TRUE_FALSE = 'TF', 'True/False'
        MULTIPLE_ANSWER = 'MA', 'Multiple Answer'
        TEXT = 'TXT', 'Text'

    quiz = models.ForeignKey(Quiz, related_name='questions', on_delete=models.CASCADE)
    text = models.TextField()
    points = models.PositiveSmallIntegerField(default=1)
    type = models.CharField(max_length=3, choices=Types.choices, default=Types.MULTIPLE_CHOICE)

    def __str__(self):
        return self.text
    
class Option(models.Model):
    question = models.ForeignKey(Question, related_name='options', on_delete=models.CASCADE)
    text = models.CharField(max_length=512)
    is_correct = models.BooleanField(default=False)

    def __str__(self):
        return self.text
    
class Submission(models.Model):
    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE)
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    answers = models.JSONField()
    score = models.PositiveSmallIntegerField(default=0)
    submitted_at = models.DateTimeField(auto_now_add=True)
    graded_at = models.DateTimeField(null=True)

    def __str__(self):
        return f"{self.student.email} - {self.quiz.title} - {self.score}"
