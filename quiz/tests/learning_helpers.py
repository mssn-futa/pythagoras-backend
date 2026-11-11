"""Fixtures for Arabic class/module tests: classes, enrollments and dawah resources."""
from datetime import datetime, timedelta, timezone as dt_timezone
from itertools import count

from dawah.models import Event, EventResource, PublicationStatus
from quiz import lifecycle
from quiz.models import Course, Enrollment, LearningModule, ModuleResource, Quiz

from .helpers import QuizAPITestMixin, make_quiz

T0 = datetime(2026, 9, 28, 17, 0, tzinfo=dt_timezone.utc)
PUBLISHED = PublicationStatus.PUBLISHED
_keys = count(1)


def make_event(status=PUBLISHED, **fields):
    defaults = {'title': 'Arabic circle', 'category': 'special_lecture', 'tags': 'arabic',
                'speakers': 'Ustadh Musa', 'location': 'Hall', 'summary': 'Greetings'}
    defaults.update(fields)
    return Event.objects.create(status=status, start_time=T0, end_time=T0 + timedelta(hours=1),
                                **defaults)


def make_audio(event, status=PUBLISHED, ready=True, **fields):
    """An audio resource; ``ready`` gives it a validated, promoted asset (as DAWAH-02 would)."""
    fields.setdefault('title', 'Greetings audio')
    if ready:
        key = next(_keys)
        fields.update(storage_status=EventResource.StorageStatus.READY,
                      private_object_key=f'media/audio/test-{key}.mp3', etag='e',
                      content_type='audio/mpeg', size_bytes=1000, file_name='greetings.mp3',
                      public_object_key=f'public/audio/test-{key}.mp3',
                      public_media_url=f'https://media.example.test/audio/test-{key}.mp3',
                      uploaded_at=T0)
    return EventResource.objects.create(event=event, type='aud', status=status, **fields)


def make_video(event, status=PUBLISHED, video_id='abcdefghijk', **fields):
    fields.setdefault('title', 'Greetings video')
    return EventResource.objects.create(
        event=event, type='vid', status=status, youtube_video_id=video_id,
        storage_status=EventResource.StorageStatus.NOT_APPLICABLE, **fields)


def make_class(status=Course.Status.DRAFT, code='ARB101', title='Arabic for beginners', **fields):
    course = Course.objects.create(kind=Course.Kind.ARABIC, code=code, title=title, **fields)
    if status != Course.Status.DRAFT:
        Course.objects.filter(pk=course.pk).update(status=Course.Status.PUBLISHED,
                                                   published_at=T0)
    if status == Course.Status.CLOSED:
        Course.objects.filter(pk=course.pk).update(status=Course.Status.CLOSED, closed_at=T0)
    course.refresh_from_db()
    return course


def make_module(course, order, status=LearningModule.Status.PUBLISHED, resources=(), **fields):
    fields.setdefault('title', f'Module {order}')
    fields.setdefault('body', f'Lesson {order}')
    module = LearningModule.objects.create(course=course, order=order, status=status, **fields)
    ModuleResource.objects.bulk_create([
        ModuleResource(module=module, resource=resource, position=index)
        for index, resource in enumerate(resources, start=1)])
    return module


def enroll(course, student, status=Enrollment.Status.ACTIVE):
    return Enrollment.objects.create(
        course=course, student=student, status=status, enrolled_at=T0,
        withdrawn_at=T0 if status == Enrollment.Status.WITHDRAWN else None)


class LearningAPITestMixin(QuizAPITestMixin):

    def data(self, response, status_code=200):
        self.assertEqual(response.status_code, status_code, response.content)
        body = response.json()
        self.assertIs(body['success'], True)
        return body['data']

    def results(self, response):
        page = self.data(response)
        self.assertEqual(set(page), {'count', 'next', 'previous', 'results'})
        return page['results']


def linked_quiz(title, course, status='published'):
    """A quiz whose ``related_course`` is ``course`` (linked while a draft, as authoring does)."""
    quiz, _ = make_quiz(title, active=False)
    Quiz.objects.filter(pk=quiz.pk).update(related_course=course)
    if status != 'draft':
        lifecycle.publish_quiz(quiz.pk)
    if status == 'closed':
        lifecycle.close_quiz(quiz.pk)
    quiz.refresh_from_db()
    return quiz
