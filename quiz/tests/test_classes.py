"""ASSESS-07 Arabic class, enrollment, roster and module API (PostgreSQL)."""
from datetime import timedelta

from django.db import transaction
from django.db.models import ProtectedError
from django.test import TestCase
from django.utils import timezone

from dawah.models import EventResource, PublicationStatus
from quiz.models import Course, Enrollment, LearningModule, ModuleResource, Quiz

from .helpers import make_quiz
from .learning_helpers import (
    LearningAPITestMixin, enroll, linked_quiz, make_audio, make_class, make_event, make_module, make_video,
)

DRAFT, PUBLISHED, CLOSED = Course.Status.DRAFT, Course.Status.PUBLISHED, Course.Status.CLOSED
ACTIVE, WITHDRAWN = Enrollment.Status.ACTIVE, Enrollment.Status.WITHDRAWN


class ClassAdminTests(LearningAPITestMixin, TestCase):

    def test_create_draft_class(self):
        data = self.data(self.call(self.admin, 'post', '/quiz/classes/', {
            'code': 'ARB101', 'title': 'Arabic for beginners', 'description': 'Weekly'}), 201)
        self.assertEqual({k: data[k] for k in ('code', 'title', 'description', 'kind', 'status',
                                               'module_count', 'enrollment_count')},
                         {'code': 'ARB101', 'title': 'Arabic for beginners',
                          'description': 'Weekly', 'kind': 'arabic', 'status': 'draft',
                          'module_count': 0, 'enrollment_count': 0})
        self.assertTrue(data['created_at'].endswith('Z'))
        self.assertIsNone(data['published_at'])
        course = Course.objects.get(pk=data['id'])
        self.assertEqual((course.kind, course.department, course.level, course.semester),
                         ('arabic', '', '', ''))

    def test_create_validation(self):
        for body, field in [
            ({'title': 'T'}, 'code'),
            ({'code': '  ', 'title': 'T'}, 'code'),
            ({'code': 'TOOLONG1', 'title': 'T'}, 'code'),
            ({'code': 101, 'title': 'T'}, 'code'),
            ({'code': 'A1', 'title': ''}, 'title'),
            ({'code': 'A1', 'title': 'T', 'kind': 'general'}, 'kind'),
            ({'code': 'A1', 'title': 'T', 'status': 'published'}, 'status'),
            ({'code': 'A1', 'title': 'T', 'department': 'X'}, 'department'),
        ]:
            with self.subTest(body=body):
                response = self.call(self.admin, 'post', '/quiz/classes/', body)
                body_json = self.assertEnvelopeError(response, 400, 'VALIDATION_ERROR')
                self.assertIn(field, body_json['data']['fields'])
        self.assertFalse(Course.objects.exists())

    def test_admin_list_status_filter_and_legacy_courses_hidden(self):
        legacy = Course.objects.create(code='MTH101', title='Maths', department='Maths',
                                       level='100', semester='First')
        draft = make_class(code='D1')
        published = make_class(PUBLISHED, code='P1')
        closed = make_class(CLOSED, code='C1')
        rows = self.results(self.call(self.admin, 'get', '/quiz/classes/'))
        self.assertEqual([row['id'] for row in rows], [closed.pk, published.pk, draft.pk])
        rows = self.results(self.call(self.admin, 'get', '/quiz/classes/?status=published'))
        self.assertEqual([row['id'] for row in rows], [published.pk])
        self.assertEnvelopeError(self.call(self.admin, 'get', '/quiz/classes/?status=x'),
                                 400, 'VALIDATION_ERROR')
        for method, path in [('get', ''), ('patch', ''), ('delete', ''), ('post', 'publish/'),
                             ('post', 'close/'), ('get', 'modules/'), ('get', 'enrollments/'),
                             ('post', 'modules/')]:
            with self.subTest(method=method, path=path):
                body = {'title': 'x'} if method in ('patch', 'post') and path in ('', 'modules/') \
                    else None
                response = self.call(self.admin, method, f'/quiz/classes/{legacy.pk}/{path}', body)
                self.assertEnvelopeError(response, 404, 'NOT_FOUND')
        legacy.refresh_from_db()
        self.assertEqual((legacy.kind, legacy.title, legacy.status), ('general', 'Maths', 'draft'))

    def test_patch_draft_and_published_but_not_closed(self):
        for status in (DRAFT, PUBLISHED):
            course = make_class(status, code=f'X{status[:3]}')
            data = self.data(self.call(self.admin, 'patch', f'/quiz/classes/{course.pk}/',
                                       {'title': 'Renamed', 'description': 'New'}))
            self.assertEqual((data['title'], data['description'], data['status']),
                             ('Renamed', 'New', status))
        closed = make_class(CLOSED, code='C1')
        self.assertEnvelopeError(self.call(self.admin, 'patch', f'/quiz/classes/{closed.pk}/',
                                           {'title': 'Renamed'}), 409, 'CLASS_CLOSED')
        # Echoing unchanged values on a closed class is harmless.
        self.data(self.call(self.admin, 'patch', f'/quiz/classes/{closed.pk}/',
                            {'title': closed.title}))
        self.assertEnvelopeError(self.call(self.admin, 'patch', f'/quiz/classes/{closed.pk}/',
                                           {'status': 'draft'}), 400, 'VALIDATION_ERROR')

    def test_lifecycle(self):
        course = make_class()
        path = f'/quiz/classes/{course.pk}/'
        self.assertEnvelopeError(self.call(self.admin, 'post', path + 'close/'), 409,
                                 'INVALID_TRANSITION')
        self.assertEnvelopeError(self.call(self.admin, 'post', path + 'publish/', {'x': 1}),
                                 400, 'VALIDATION_ERROR')
        published = self.data(self.call(self.admin, 'post', path + 'publish/'))
        self.assertEqual(published['status'], 'published')
        self.assertIsNotNone(published['published_at'])
        again = self.data(self.call(self.admin, 'post', path + 'publish/'))
        self.assertEqual(again['published_at'], published['published_at'])
        closed = self.data(self.call(self.admin, 'post', path + 'close/'))
        self.assertEqual(closed['status'], 'closed')
        self.assertIsNotNone(closed['closed_at'])
        self.assertEqual(self.data(self.call(self.admin, 'post', path + 'close/'))['closed_at'],
                         closed['closed_at'])
        self.assertEnvelopeError(self.call(self.admin, 'post', path + 'publish/'), 409,
                                 'INVALID_TRANSITION')
        self.assertEnvelopeError(self.call(self.admin, 'delete', path), 409, 'INVALID_TRANSITION')

    def test_delete_draft_only_when_unreferenced(self):
        course = make_class()
        module = make_module(course, 1, resources=[make_audio(make_event())])
        response = self.call(self.admin, 'delete', f'/quiz/classes/{course.pk}/')
        self.assertEqual(response.status_code, 204)
        self.assertFalse(LearningModule.objects.filter(pk=module.pk).exists())
        self.assertEqual(EventResource.objects.count(), 1)  # resources are never cascaded

        linked = make_class(code='L1')
        quiz, _ = make_quiz('Linked', active=False)
        Quiz.objects.filter(pk=quiz.pk).update(related_course=linked)
        self.assertEnvelopeError(self.call(self.admin, 'delete', f'/quiz/classes/{linked.pk}/'),
                                 409, 'RESOURCE_REFERENCED')
        self.assertTrue(Course.objects.filter(pk=linked.pk).exists())

        published = make_class(PUBLISHED, code='P1')
        self.assertEnvelopeError(self.call(self.admin, 'delete',
                                           f'/quiz/classes/{published.pk}/'),
                                 409, 'INVALID_TRANSITION')

    def test_enrollment_history_is_protected(self):
        course = make_class(PUBLISHED)
        record = enroll(course, self.student)
        for operation in (course.delete, self.student.delete):
            with self.subTest(operation=operation), self.assertRaises(ProtectedError), \
                    transaction.atomic():
                operation()
        self.assertTrue(Enrollment.objects.filter(pk=record.pk).exists())

    def test_counts(self):
        course = make_class(PUBLISHED)
        make_module(course, 1)
        make_module(course, 2, status=LearningModule.Status.DRAFT)
        enroll(course, self.student)
        enroll(course, self.other_student, WITHDRAWN)
        admin_row = self.data(self.call(self.admin, 'get', f'/quiz/classes/{course.pk}/'))
        self.assertEqual((admin_row['module_count'], admin_row['enrollment_count']), (2, 1))
        student_row = self.data(self.call(self.student, 'get', f'/quiz/classes/{course.pk}/'))
        self.assertEqual(student_row['module_count'], 1)
        self.assertNotIn('enrollment_count', student_row)


class PermissionMatrixTests(LearningAPITestMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.course = make_class(PUBLISHED)
        self.module = make_module(self.course, 1, status=LearningModule.Status.DRAFT)
        base = f'/quiz/classes/{self.course.pk}/'
        mod = f'{base}modules/{self.module.pk}/'
        self.admin_requests = [
            ('post', '/quiz/classes/', {'code': 'N1', 'title': 'New'}),
            ('patch', base, {'title': 'x'}),
            ('delete', base, None),
            ('post', base + 'publish/', None),
            ('post', base + 'close/', None),
            ('get', base + 'enrollments/', None),
            ('post', base + 'enrollments/', {'student_id': self.student.pk}),
            ('post', base + 'modules/', {'title': 'M'}),
            ('post', base + 'modules/reorder/', {'module_ids': [self.module.pk]}),
            ('patch', mod, {'title': 'x'}),
            ('delete', mod, None),
            ('post', mod + 'publish/', None),
            ('post', mod + 'unpublish/', None),
        ]
        self.student_requests = [
            ('get', base + 'enrollment/', None),
            ('post', base + 'enrollment/', {}),
            ('delete', base + 'enrollment/', None),
        ]
        self.read_requests = [
            ('get', '/quiz/classes/', None), ('get', base, None),
            ('get', base + 'modules/', None), ('get', mod, None),
        ]

    def snapshot(self):
        return (list(Course.objects.order_by('pk').values_list('pk', 'title', 'status')),
                list(LearningModule.objects.order_by('pk').values_list('pk', 'title', 'status')),
                list(Enrollment.objects.order_by('pk').values_list('pk', 'status')))

    def test_anonymous_and_inactive_are_refused_everywhere(self):
        before = self.snapshot()
        for user, status_code in [(None, 401), (self.inactive_student, 401),
                                  (self.inactive_admin, 401)]:
            for method, path, body in (self.admin_requests + self.student_requests
                                       + self.read_requests):
                with self.subTest(user=user, method=method, path=path):
                    response = self.call(user, method, path, body)
                    self.assertIn(response.status_code, (401, 403), response.content)
        self.assertEqual(self.snapshot(), before)

    def test_students_cannot_use_admin_routes(self):
        before = self.snapshot()
        for user in (self.student, self.staff_student):
            for method, path, body in self.admin_requests:
                with self.subTest(user=user.email, method=method, path=path):
                    self.assertEnvelopeError(self.call(user, method, path, body), 403,
                                             'PERMISSION_DENIED')
        self.assertEqual(self.snapshot(), before)

    def test_admins_have_no_own_enrollment(self):
        for method, path, body in self.student_requests:
            with self.subTest(method=method):
                self.assertEnvelopeError(self.call(self.admin, method, path, body), 403,
                                         'PERMISSION_DENIED')
        self.assertFalse(Enrollment.objects.exists())

    def test_admin_previews_drafts(self):
        draft = make_class(code='D1')
        module = make_module(draft, 1, status=LearningModule.Status.DRAFT)
        self.data(self.call(self.admin, 'get', f'/quiz/classes/{draft.pk}/'))
        rows = self.results(self.call(self.admin, 'get', f'/quiz/classes/{draft.pk}/modules/'))
        self.assertEqual([row['id'] for row in rows], [module.pk])
        detail = self.data(self.call(self.admin, 'get',
                                     f'/quiz/classes/{draft.pk}/modules/{module.pk}/'))
        self.assertEqual(detail['status'], 'draft')


class StudentCatalogTests(LearningAPITestMixin, TestCase):

    def test_catalog_lists_published_arabic_classes_only(self):
        Course.objects.create(code='MTH101', title='Maths', department='Maths', level='100',
                              semester='First')
        make_class(code='D1')
        b = make_class(PUBLISHED, code='B1')
        a = make_class(PUBLISHED, code='A1')
        closed = make_class(CLOSED, code='C1')
        rows = self.results(self.call(self.student, 'get', '/quiz/classes/?status=draft'))
        self.assertEqual([row['id'] for row in rows], [a.pk, b.pk])
        self.assertEqual(set(rows[0]), {'id', 'code', 'title', 'description', 'kind', 'status',
                                        'published_at', 'closed_at', 'created_at', 'updated_at',
                                        'module_count'})
        self.data(self.call(self.student, 'get', f'/quiz/classes/{closed.pk}/'))

    def test_draft_and_legacy_details_are_404(self):
        draft = make_class()
        legacy = Course.objects.create(code='MTH101', title='Maths', department='Maths',
                                       level='100', semester='First')
        for course in (draft, legacy):
            for suffix in ('', 'enrollment/', 'modules/'):
                with self.subTest(course=course.code, suffix=suffix):
                    self.assertEnvelopeError(
                        self.call(self.student, 'get', f'/quiz/classes/{course.pk}/{suffix}'),
                        404, 'NOT_FOUND')
        self.assertEnvelopeError(self.call(self.student, 'post',
                                           f'/quiz/classes/{draft.pk}/enrollment/', {}),
                                 404, 'NOT_FOUND')
        self.assertFalse(Enrollment.objects.exists())


class OwnEnrollmentTests(LearningAPITestMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.course = make_class(PUBLISHED)
        self.path = f'/quiz/classes/{self.course.pk}/enrollment/'

    def test_enroll_withdraw_reactivate_reuses_the_record(self):
        self.assertEnvelopeError(self.call(self.student, 'get', self.path), 404, 'NOT_FOUND')
        self.assertEnvelopeError(self.call(self.student, 'delete', self.path), 404, 'NOT_FOUND')
        response = self.call(self.student, 'post', self.path, {})
        created = self.data(response, 201)
        self.assertEqual(response.json()['message'], 'Enrolled.')
        self.assertEqual(set(created), {'id', 'course_id', 'student_id', 'status',
                                        'enrolled_at', 'withdrawn_at'})
        self.assertEqual((created['course_id'], created['student_id'], created['status'],
                          created['withdrawn_at']),
                         (self.course.pk, self.student.pk, 'active', None))
        replay = self.data(self.call(self.student, 'post', self.path))
        self.assertEqual(replay, created)
        self.assertEqual(self.data(self.call(self.student, 'get', self.path)), created)

        withdrawn = self.data(self.call(self.student, 'delete', self.path))
        self.assertEqual((withdrawn['id'], withdrawn['status']), (created['id'], 'withdrawn'))
        self.assertIsNotNone(withdrawn['withdrawn_at'])
        self.assertEqual(self.data(self.call(self.student, 'delete', self.path)), withdrawn)

        back = self.data(self.call(self.student, 'post', self.path, {}))
        self.assertEqual((back['id'], back['status'], back['withdrawn_at']),
                         (created['id'], 'active', None))
        self.assertEqual(Enrollment.objects.count(), 1)

    def test_body_and_identity_are_not_accepted(self):
        self.assertEnvelopeError(self.call(self.student, 'post', self.path,
                                           {'student_id': self.other_student.pk}),
                                 400, 'VALIDATION_ERROR')
        self.assertFalse(Enrollment.objects.exists())

    def test_students_only_see_their_own_record(self):
        enroll(self.course, self.other_student)
        self.assertEnvelopeError(self.call(self.student, 'get', self.path), 404, 'NOT_FOUND')

    def test_closed_class(self):
        self.course.status = CLOSED
        Course.objects.filter(pk=self.course.pk).update(status=CLOSED,
                                                        closed_at=timezone.now())
        self.assertEnvelopeError(self.call(self.student, 'post', self.path, {}), 409,
                                 'CLASS_CLOSED')
        active = enroll(self.course, self.other_student)
        # An active record is returned unchanged by an enroll replay.
        self.assertEqual(self.data(self.call(self.other_student, 'post', self.path))['id'],
                         active.pk)
        # Withdrawing from a closed class works normally (lead decision) and is idempotent.
        withdrawn_data = self.data(self.call(self.other_student, 'delete', self.path))
        self.assertEqual((withdrawn_data['id'], withdrawn_data['status']),
                         (active.pk, 'withdrawn'))
        self.assertIsNotNone(withdrawn_data['withdrawn_at'])
        self.assertEqual(self.data(self.call(self.other_student, 'delete', self.path)),
                         withdrawn_data)
        self.assertEqual(self.data(self.call(self.other_student, 'get', self.path)),
                         withdrawn_data)
        # Only (re-)enrolling into a closed class is refused.
        self.assertEnvelopeError(self.call(self.other_student, 'post', self.path), 409,
                                 'CLASS_CLOSED')
        self.assertEqual(Enrollment.objects.get(pk=active.pk).status, WITHDRAWN)
        withdrawn = enroll(self.course, self.add_student('w@example.test'), WITHDRAWN)
        self.assertEqual(self.data(self.call(withdrawn.student, 'delete', self.path))['status'],
                         'withdrawn')
        self.assertEnvelopeError(self.call(withdrawn.student, 'post', self.path), 409,
                                 'CLASS_CLOSED')

    def test_enrollment_does_not_change_quiz_eligibility(self):
        quiz = linked_quiz('Class quiz', self.course)
        response = self.start_api(self.student, quiz.pk)  # never enrolled
        self.assertEqual(response.status_code, 201, response.content)


class RosterTests(LearningAPITestMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.course = make_class(PUBLISHED)
        self.path = f'/quiz/classes/{self.course.pk}/enrollments/'

    def post(self, body):
        return self.call(self.admin, 'post', self.path, body)

    def test_enroll_withdraw_reinstate_is_idempotent(self):
        created = self.data(self.post({'student_id': self.student.pk}), 201)
        self.assertEqual(set(created), {'id', 'course_id', 'student_id', 'student_name',
                                        'student_email', 'status', 'enrolled_at',
                                        'withdrawn_at', 'updated_at'})
        self.assertEqual((created['status'], created['student_name'], created['student_email']),
                         ('active', 'Amina Yusuf', 'student@example.test'))
        self.assertEqual(self.data(self.post({'student_id': self.student.pk,
                                              'status': 'active'}))['id'], created['id'])
        withdrawn = self.data(self.post({'student_id': self.student.pk, 'status': 'withdrawn'}))
        self.assertEqual((withdrawn['id'], withdrawn['status']), (created['id'], 'withdrawn'))
        again = self.data(self.post({'student_id': self.student.pk, 'status': 'withdrawn'}))
        self.assertEqual(again['withdrawn_at'], withdrawn['withdrawn_at'])
        back = self.data(self.post({'student_id': self.student.pk, 'status': 'active'}))
        self.assertEqual((back['id'], back['status'], back['withdrawn_at']),
                         (created['id'], 'active', None))
        self.assertEqual(Enrollment.objects.count(), 1)

    def test_validation(self):
        for body, field in [
            ({}, 'student_id'),
            ({'student_id': str(self.student.pk)}, 'student_id'),
            ({'student_id': self.student.pk, 'status': 'gone'}, 'status'),
            ({'student_id': self.student.pk, 'course': 1}, 'course'),
            ({'student_id': self.admin.pk}, 'student_id'),
            ({'student_id': 999999}, 'student_id'),
            ({'student_id': self.inactive_student.pk}, 'student_id'),
            ({'student_id': self.student.pk, 'status': 'withdrawn'}, 'student_id'),
        ]:
            with self.subTest(body=body):
                body_json = self.assertEnvelopeError(self.post(body), 400, 'VALIDATION_ERROR')
                self.assertIn(field, body_json['data']['fields'])
        self.assertFalse(Enrollment.objects.exists())

    def test_null_characters_in_query_parameters_are_field_errors(self):
        enroll(self.course, self.student)
        module_path = f'/quiz/classes/{self.course.pk}/modules/'
        for user, path, field in [
            (self.admin, f'{self.path}?search=%00', 'search'),
            (self.admin, f'{self.path}?search=ami%00na', 'search'),
            (self.admin, f'{self.path}?status=active%00', 'status'),
            (self.admin, f'{self.path}?page=%00', 'page'),
            (self.admin, f'{self.path}?page_size=1%00', 'page_size'),
            (self.admin, '/quiz/classes/?status=%00', 'status'),
            (self.student, '/quiz/classes/?page=%00', 'page'),
            (self.admin, f'{module_path}?status=draft%00', 'status'),
            (self.student, f'{module_path}?page_size=%00', 'page_size'),
        ]:
            with self.subTest(path=path):
                body = self.assertEnvelopeError(self.call(user, 'get', path), 400,
                                                'VALIDATION_ERROR')
                self.assertEqual(body['data']['fields'],
                                 {field: ['Null characters are not allowed.']})

    def test_inactive_student_can_still_be_withdrawn(self):
        record = enroll(self.course, self.inactive_student)
        data = self.data(self.post({'student_id': self.inactive_student.pk,
                                    'status': 'withdrawn'}))
        self.assertEqual((data['id'], data['status']), (record.pk, 'withdrawn'))

    def test_draft_and_closed_classes(self):
        draft = make_class(code='D1')
        self.assertEnvelopeError(self.call(self.admin, 'post',
                                           f'/quiz/classes/{draft.pk}/enrollments/',
                                           {'student_id': self.student.pk}),
                                 409, 'CLASS_NOT_PUBLISHED')
        closed = make_class(CLOSED, code='C1')
        record = enroll(closed, self.student)
        path = f'/quiz/classes/{closed.pk}/enrollments/'
        self.assertEnvelopeError(self.call(self.admin, 'post', path,
                                           {'student_id': self.other_student.pk}),
                                 409, 'CLASS_CLOSED')
        self.assertEnvelopeError(self.call(self.admin, 'post', path,
                                           {'student_id': self.student.pk,
                                            'status': 'withdrawn'}), 409, 'CLASS_CLOSED')
        rows = self.results(self.call(self.admin, 'get', path))
        self.assertEqual([(row['id'], row['status']) for row in rows], [(record.pk, 'active')])

    def test_list_search_status_and_pagination(self):
        zainab = self.add_student('zainab@example.test')
        zainab.first_name, zainab.last_name = 'Zainab', 'Bello'
        zainab.save()
        a = enroll(self.course, self.student)
        b = enroll(self.course, zainab, WITHDRAWN)
        enroll(make_class(PUBLISHED, code='OTHER'), self.other_student)
        rows = self.results(self.call(self.admin, 'get', self.path))
        self.assertEqual([row['id'] for row in rows], [a.pk, b.pk])
        for query, expected in [('search=zain', [b.pk]), ('search=BELLO', [b.pk]),
                                ('search=Zainab%20Bel', [b.pk]),
                                ('search=student@example', [a.pk]),
                                ('status=withdrawn', [b.pk]), ('status=active', [a.pk]),
                                ('search=amina&status=withdrawn', [])]:
            with self.subTest(query=query):
                rows = self.results(self.call(self.admin, 'get', f'{self.path}?{query}'))
                self.assertEqual([row['id'] for row in rows], expected)
        page = self.data(self.call(self.admin, 'get', f'{self.path}?page_size=1'))
        self.assertEqual((page['count'], len(page['results'])), (2, 1))
        self.assertIsNotNone(page['next'])
        self.assertEnvelopeError(self.call(self.admin, 'get', f'{self.path}?status=x'), 400,
                                 'VALIDATION_ERROR')


class ModuleAdminTests(LearningAPITestMixin, TestCase):

    def setUp(self):
        super().setUp()
        self.course = make_class(PUBLISHED)
        self.base = f'/quiz/classes/{self.course.pk}/modules/'
        self.event = make_event()
        self.audio = make_audio(self.event)
        self.video = make_video(self.event)

    def create(self, body):
        return self.call(self.admin, 'post', self.base, body)

    def hidden_resources(self):
        draft_event = make_event(status=PublicationStatus.DRAFT)
        return {
            'draft resource': make_audio(self.event, status=PublicationStatus.DRAFT),
            'unpublished resource': make_audio(self.event, status=PublicationStatus.UNPUBLISHED),
            'draft parent event': make_audio(draft_event),
            'audio without asset': make_audio(self.event, ready=False),
            'video without reference': make_video(self.event, video_id=''),
        }

    def test_create_with_visible_resources(self):
        response = self.create({'title': 'Arabic greetings', 'body': 'Lesson text', 'order': 1,
                                'resource_ids': [self.video.pk, self.audio.pk]})
        data = self.data(response, 201)
        self.assertEqual({k: data[k] for k in ('course_id', 'title', 'body', 'order', 'status',
                                               'resource_ids', 'hidden_resource_ids')},
                         {'course_id': self.course.pk, 'title': 'Arabic greetings',
                          'body': 'Lesson text', 'order': 1, 'status': 'draft',
                          'resource_ids': [self.video.pk, self.audio.pk],
                          'hidden_resource_ids': []})
        self.assertEqual([item['id'] for item in data['resources']],
                         [self.video.pk, self.audio.pk])
        self.assertEqual(data['resources'][0]['playback']['video_id'], 'abcdefghijk')
        self.assertEqual(data['resources'][1]['event']['title'], 'Arabic circle')
        public = self.data(self.call(None, 'get', f'/dawah/resources/{self.audio.pk}/'))
        self.assertEqual(data['resources'][1], public)
        self.assertEqual(data['quizzes'], [])

    def test_defaults_and_order(self):
        first = self.data(self.create({'title': 'One'}), 201)
        self.assertEqual((first['order'], first['body'], first['resource_ids']), (1, '', []))
        self.data(self.create({'title': 'Five', 'order': 5}), 201)
        self.assertEqual(self.data(self.create({'title': 'Six'}), 201)['order'], 6)
        body = self.assertEnvelopeError(self.create({'title': 'Clash', 'order': 5}), 400,
                                        'VALIDATION_ERROR')
        self.assertIn('order', body['data']['fields'])

    def test_create_validation(self):
        for body, field in [
            ({}, 'title'), ({'title': ' '}, 'title'), ({'title': 'x' * 251}, 'title'),
            ({'title': 'T', 'order': 0}, 'order'), ({'title': 'T', 'order': '1'}, 'order'),
            ({'title': 'T', 'order': 10001}, 'order'), ({'title': 'T', 'body': 5}, 'body'),
            ({'title': 'T', 'resource_ids': 'x'}, 'resource_ids'),
            ({'title': 'T', 'resource_ids': ['1']}, 'resource_ids.0'),
            ({'title': 'T', 'resource_ids': [self.audio.pk, self.audio.pk]}, 'resource_ids'),
            ({'title': 'T', 'resource_ids': [999999]}, 'resource_ids'),
            ({'title': 'T', 'status': 'published'}, 'status'),
            ({'title': 'T', 'course': self.course.pk}, 'course'),
        ]:
            with self.subTest(body=body):
                body_json = self.assertEnvelopeError(self.create(body), 400, 'VALIDATION_ERROR')
                self.assertIn(field, body_json['data']['fields'])
        self.assertFalse(LearningModule.objects.exists())

    def test_hidden_resources_cannot_be_attached(self):
        for name, resource in self.hidden_resources().items():
            with self.subTest(name):
                body = self.assertEnvelopeError(
                    self.create({'title': 'T', 'resource_ids': [self.audio.pk, resource.pk]}),
                    400, 'VALIDATION_ERROR')
                self.assertEqual(body['data']['fields']['resource_ids'],
                                 [f'Resource {resource.pk} is not published or not available.'])
        self.assertFalse(LearningModule.objects.exists())
        module = make_module(self.course, 1, resources=[self.audio])
        hidden = self.hidden_resources()['draft resource']
        self.assertEnvelopeError(self.call(self.admin, 'patch', f'{self.base}{module.pk}/',
                                           {'resource_ids': [self.audio.pk, hidden.pk]}),
                                 400, 'VALIDATION_ERROR')
        self.assertEqual(list(module.attachments.values_list('resource_id', flat=True)),
                         [self.audio.pk])

    def test_already_attached_hidden_resources_never_block_edits(self):
        module = make_module(self.course, 1, resources=[self.audio, self.video])
        EventResource.objects.filter(pk=self.audio.pk).update(
            status=PublicationStatus.UNPUBLISHED)
        path = f'{self.base}{module.pk}/'
        data = self.data(self.call(self.admin, 'patch', path, {'title': 'Renamed'}))
        self.assertEqual((data['resource_ids'], data['hidden_resource_ids']),
                         ([self.audio.pk, self.video.pk], [self.audio.pk]))
        self.assertEqual([item['id'] for item in data['resources']], [self.video.pk])
        # Re-sending the set (reordered, plus a new visible one) keeps the hidden one.
        extra = make_audio(self.event, title='Extra')
        data = self.data(self.call(self.admin, 'patch', path, {
            'resource_ids': [extra.pk, self.video.pk, self.audio.pk]}))
        self.assertEqual(data['resource_ids'], [extra.pk, self.video.pk, self.audio.pk])
        self.assertEqual(data['hidden_resource_ids'], [self.audio.pk])
        # Detaching it.
        data = self.data(self.call(self.admin, 'patch', path, {'resource_ids': [self.video.pk]}))
        self.assertEqual((data['resource_ids'], data['hidden_resource_ids']),
                         ([self.video.pk], []))

    def test_partial_patch(self):
        module = make_module(self.course, 1, status=LearningModule.Status.DRAFT,
                             resources=[self.audio])
        make_module(self.course, 2)
        path = f'{self.base}{module.pk}/'
        data = self.data(self.call(self.admin, 'patch', path, {'order': 3}))
        self.assertEqual((data['order'], data['title'], data['resource_ids']),
                         (3, 'Module 1', [self.audio.pk]))
        self.assertEnvelopeError(self.call(self.admin, 'patch', path, {'order': 2}), 400,
                                 'VALIDATION_ERROR')
        self.data(self.call(self.admin, 'patch', path, {'order': 3}))  # own order is fine
        data = self.data(self.call(self.admin, 'patch', path, {'body': '  spaced  '}))
        self.assertEqual(data['body'], '  spaced  ')
        self.assertEnvelopeError(self.call(self.admin, 'patch', path, {'title': ''}), 400,
                                 'VALIDATION_ERROR')

    def test_publish_unpublish_delete(self):
        module = make_module(self.course, 1, status=LearningModule.Status.DRAFT,
                             resources=[self.audio])
        path = f'{self.base}{module.pk}/'
        published = self.data(self.call(self.admin, 'post', path + 'publish/'))
        self.assertEqual(published['status'], 'published')
        self.assertIsNotNone(published['published_at'])
        self.assertEqual(self.data(self.call(self.admin, 'post', path + 'publish/'))['status'],
                         'published')
        self.assertEnvelopeError(self.call(self.admin, 'delete', path), 409, 'UNPUBLISH_REQUIRED')
        self.assertEqual(self.data(self.call(self.admin, 'post', path + 'unpublish/'))['status'],
                         'draft')
        self.assertEqual(self.data(self.call(self.admin, 'post', path + 'unpublish/'))['status'],
                         'draft')
        self.assertEqual(self.call(self.admin, 'delete', path).status_code, 204)
        self.assertFalse(ModuleResource.objects.exists())
        self.assertTrue(EventResource.objects.filter(pk=self.audio.pk).exists())

    def test_closed_class_is_read_only(self):
        module = make_module(self.course, 1, status=LearningModule.Status.DRAFT)
        Course.objects.filter(pk=self.course.pk).update(status=CLOSED, closed_at=timezone.now())
        path = f'{self.base}{module.pk}/'
        for method, url, body in [('post', self.base, {'title': 'New'}),
                                  ('patch', path, {'title': 'Renamed'}),
                                  ('delete', path, None), ('post', path + 'publish/', None),
                                  ('post', self.base + 'reorder/', {'module_ids': [module.pk]})]:
            with self.subTest(method=method, url=url):
                self.assertEnvelopeError(self.call(self.admin, method, url, body), 409,
                                         'CLASS_CLOSED')
        self.data(self.call(self.admin, 'get', path))
        self.assertEqual(LearningModule.objects.get().title, 'Module 1')

    def test_module_belongs_to_its_url_class(self):
        other = make_class(PUBLISHED, code='OTHER')
        foreign = make_module(other, 1)
        for method, suffix, body in [('get', '', None), ('patch', '', {'title': 'x'}),
                                     ('delete', '', None), ('post', 'publish/', None),
                                     ('post', 'unpublish/', None)]:
            with self.subTest(method=method, suffix=suffix):
                self.assertEnvelopeError(
                    self.call(self.admin, method, f'{self.base}{foreign.pk}/{suffix}', body),
                    404, 'NOT_FOUND')
        foreign.refresh_from_db()
        self.assertEqual((foreign.title, foreign.status), ('Module 1', 'published'))

    def test_reorder(self):
        a, b, c = (make_module(self.course, n) for n in (1, 2, 5))
        other = make_module(make_class(PUBLISHED, code='OTHER'), 1)
        path = self.base + 'reorder/'
        for body in [{'module_ids': [a.pk, b.pk]}, {'module_ids': [a.pk, b.pk, c.pk, other.pk]},
                     {'module_ids': [a.pk, b.pk, b.pk]},
                     {'module_ids': [a.pk, b.pk, other.pk]}, {'module_ids': 'x'}, {}]:
            with self.subTest(body=body):
                self.assertEnvelopeError(self.call(self.admin, 'post', path, body), 400,
                                         'VALIDATION_ERROR')
        self.assertEqual(list(LearningModule.objects.filter(course=self.course)
                              .order_by('pk').values_list('order', flat=True)), [1, 2, 5])
        data = self.data(self.call(self.admin, 'post', path, {'module_ids': [c.pk, a.pk, b.pk]}))
        self.assertEqual([(item['id'], item['order']) for item in data],
                         [(c.pk, 1), (a.pk, 2), (b.pk, 3)])
        rows = self.results(self.call(self.admin, 'get', self.base))
        self.assertEqual([row['id'] for row in rows], [c.pk, a.pk, b.pk])

    def test_list_status_filter_and_pagination(self):
        draft = make_module(self.course, 2, status=LearningModule.Status.DRAFT)
        live = make_module(self.course, 1)
        rows = self.results(self.call(self.admin, 'get', self.base + '?status=draft'))
        self.assertEqual([row['id'] for row in rows], [draft.pk])
        page = self.data(self.call(self.admin, 'get', self.base + '?page_size=1'))
        self.assertEqual((page['count'], page['results'][0]['id']), (2, live.pk))

    def test_attached_resources_are_protected_from_deletion(self):
        make_module(self.course, 1, resources=[self.audio])
        with self.assertRaises(ProtectedError), transaction.atomic():
            self.audio.delete()
        with self.assertRaises(ProtectedError), transaction.atomic():
            self.event.delete()
        EventResource.objects.filter(event=self.event).update(status=PublicationStatus.DRAFT)
        self.event.__class__.objects.filter(pk=self.event.pk).update(
            status=PublicationStatus.DRAFT)
        response = self.call(self.admin, 'delete', f'/dawah/events/{self.event.pk}/')
        self.assertEnvelopeError(response, 409, 'RESOURCE_REFERENCED')
        self.assertTrue(ModuleResource.objects.filter(resource=self.audio).exists())


class QuizLinkTests(LearningAPITestMixin, TestCase):

    def test_quizzes_metadata_without_keys(self):
        course = make_class(PUBLISHED)
        module = make_module(course, 1)
        enroll(course, self.student)
        published = linked_quiz('Week 1', course)
        draft = linked_quiz('Week 2', course, 'draft')
        closed = linked_quiz('Week 0', course, 'closed')
        now = timezone.now()
        past_deadline = linked_quiz('Week -1', course, deadline=now - timedelta(hours=1),
                                    published_now=now - timedelta(days=1))
        make_quiz('Unrelated')
        # Students: only quizzes they can open now (published, deadline ahead).
        for user, expected in [
            (self.student, [published]),
            (self.admin, [published, draft, closed, past_deadline])]:
            for path in (f'/quiz/classes/{course.pk}/modules/',
                         f'/quiz/classes/{course.pk}/modules/{module.pk}/'):
                with self.subTest(user=user.email, path=path):
                    response = self.call(user, 'get', path)
                    data = self.data(response)
                    item = data['results'][0] if 'results' in data else data
                    self.assertEqual(item['quizzes'], sorted(
                        [{'id': q.pk, 'title': q.title, 'status': Quiz.objects.get(pk=q.pk).status}
                         for q in expected], key=lambda row: row['id']))
                    self.assertNoAnswerKeys(response)
                    self.assertNotIn('questions', str(item['quizzes']))
        # Every quiz a student is shown opens for that student.
        for quiz in (published,):
            self.data(self.call(self.student, 'get', f'/quiz/quizzes/{quiz.pk}/'))
        for quiz in (draft, closed, past_deadline):
            self.assertEnvelopeError(self.call(self.student, 'get', f'/quiz/quizzes/{quiz.pk}/'),
                                     404, 'NOT_FOUND')

    def test_quiz_related_course_validation(self):
        course = make_class()
        closed = make_class(CLOSED, code='C1')
        legacy = Course.objects.create(code='MTH101', title='Maths', department='Maths',
                                       level='100', semester='First')
        created = self.data(self.call(self.admin, 'post', '/quiz/quizzes/', {
            'title': 'Linked', 'related_course': course.pk}), 201)
        self.assertEqual((created['related_course'], created['course_title']),
                         (course.pk, course.title))
        path = f'/quiz/quizzes/{created["id"]}/'
        for value, message in [(legacy.pk, 'Select an Arabic class.'),
                               (closed.pk, 'This class is closed; select an open class.'),
                               ('1', None), (True, None)]:
            with self.subTest(value=value):
                body = self.assertEnvelopeError(self.call(self.admin, 'patch', path,
                                                          {'related_course': value}),
                                                400, 'VALIDATION_ERROR')
                if message:
                    self.assertEqual(body['data']['fields']['related_course'], [message])
        self.assertIsNone(self.data(self.call(self.admin, 'patch', path,
                                              {'related_course': None}))['related_course'])
        # An existing legacy link is kept on unrelated edits.
        Quiz.objects.filter(pk=created['id']).update(related_course=legacy)
        data = self.data(self.call(self.admin, 'patch', path, {'title': 'Renamed',
                                                               'related_course': legacy.pk}))
        self.assertEqual(data['related_course'], legacy.pk)


class StudentModuleAccessTests(LearningAPITestMixin, TestCase):
    """Every student module path is gated by an active enrollment in a published class."""

    def setUp(self):
        super().setUp()
        self.course = make_class(PUBLISHED)
        self.event = make_event()
        self.audio = make_audio(self.event)
        self.video = make_video(self.event)
        self.hidden = make_audio(self.event, status=PublicationStatus.UNPUBLISHED)
        self.module = make_module(self.course, 2, resources=[self.audio, self.hidden,
                                                             self.video])
        self.first = make_module(self.course, 1)
        self.draft = make_module(self.course, 3, status=LearningModule.Status.DRAFT)

    def paths(self, course=None):
        course = course or self.course
        return [f'/quiz/classes/{course.pk}/modules/',
                f'/quiz/classes/{course.pk}/modules/?page_size=100',
                f'/quiz/classes/{course.pk}/modules/{self.module.pk}/']

    def test_active_enrollment_reads_published_modules(self):
        enroll(self.course, self.student)
        response = self.call(self.student, 'get', self.paths()[1])
        rows = self.results(response)
        self.assertEqual([row['id'] for row in rows], [self.first.pk, self.module.pk])
        self.assertEqual(set(rows[1]), {'id', 'title', 'body', 'order', 'resources', 'quizzes'})
        self.assertNoAnswerKeys(response)
        detail = self.data(self.call(self.student, 'get', self.paths()[2]))
        self.assertEqual(detail, rows[1])
        self.assertEqual([item['id'] for item in detail['resources']],
                         [self.audio.pk, self.video.pk])
        for item in detail['resources']:
            public = self.data(self.call(None, 'get', f'/dawah/resources/{item["id"]}/'))
            self.assertEqual(item, public)
        self.assertNotIn('hidden_resource_ids', detail)
        self.assertNotIn(self.hidden.pk, [item['id'] for item in detail['resources']])

    def test_refused_without_active_enrollment(self):
        cases = {'never enrolled': self.student, 'withdrawn': self.other_student}
        enroll(self.course, self.other_student, WITHDRAWN)
        for name, user in cases.items():
            for path in self.paths():
                with self.subTest(name, path=path):
                    self.assertEnvelopeError(self.call(user, 'get', path), 403,
                                             'ENROLLMENT_REQUIRED')

    def test_enrollment_elsewhere_does_not_open_this_class(self):
        other = make_class(PUBLISHED, code='OTHER')
        enroll(other, self.student)
        for path in self.paths():
            with self.subTest(path=path):
                self.assertEnvelopeError(self.call(self.student, 'get', path), 403,
                                         'ENROLLMENT_REQUIRED')

    def test_draft_and_closed_classes_hide_modules(self):
        enroll(self.course, self.student)
        for status in (DRAFT, CLOSED):
            Course.objects.filter(pk=self.course.pk).update(
                status=status, closed_at=timezone.now() if status == CLOSED else None)
            for path in self.paths():
                with self.subTest(status=status, path=path):
                    self.assertEnvelopeError(self.call(self.student, 'get', path), 404,
                                             'NOT_FOUND')

    def test_draft_foreign_and_missing_modules_are_404(self):
        enroll(self.course, self.student)
        other = make_class(PUBLISHED, code='OTHER')
        foreign = make_module(other, 1)
        base = f'/quiz/classes/{self.course.pk}/modules/'
        for module_id in (self.draft.pk, foreign.pk, 999999):
            with self.subTest(module_id=module_id):
                self.assertEnvelopeError(self.call(self.student, 'get', f'{base}{module_id}/'),
                                         404, 'NOT_FOUND')

    def test_inactive_student_is_refused(self):
        enroll(self.course, self.inactive_student)
        for path in self.paths():
            with self.subTest(path=path):
                self.assertIn(self.call(self.inactive_student, 'get', path).status_code,
                              (401, 403))

    def test_access_follows_enrollment_and_publication_changes(self):
        path = self.paths()[2]
        enroll_path = f'/quiz/classes/{self.course.pk}/enrollment/'
        self.data(self.call(self.student, 'post', enroll_path), 201)
        self.data(self.call(self.student, 'get', path))
        self.data(self.call(self.student, 'delete', enroll_path))
        self.assertEnvelopeError(self.call(self.student, 'get', path), 403, 'ENROLLMENT_REQUIRED')
        self.data(self.call(self.student, 'post', enroll_path))
        self.call(self.admin, 'post', f'{path}unpublish/')
        self.assertEnvelopeError(self.call(self.student, 'get', path), 404, 'NOT_FOUND')
        self.call(self.admin, 'post', f'{path}publish/')
        self.data(self.call(self.student, 'get', path))
        self.call(self.admin, 'post', f'/quiz/classes/{self.course.pk}/close/')
        self.assertEnvelopeError(self.call(self.student, 'get', path), 404, 'NOT_FOUND')
        # History stays: the class and the enrollment record remain readable.
        self.assertEqual(self.data(self.call(self.student, 'get', enroll_path))['status'],
                         'active')

    def test_resource_visibility_changes_apply_at_read_time(self):
        enroll(self.course, self.student)
        path = self.paths()[2]
        EventResource.objects.filter(pk=self.audio.pk).update(
            status=PublicationStatus.UNPUBLISHED)
        self.assertEqual([r['id'] for r in self.data(self.call(self.student, 'get',
                                                               path))['resources']],
                         [self.video.pk])
        self.event.__class__.objects.filter(pk=self.event.pk).update(
            status=PublicationStatus.UNPUBLISHED)
        self.assertEqual(self.data(self.call(self.student, 'get', path))['resources'], [])
        admin = self.data(self.call(self.admin, 'get', path))
        self.assertEqual(admin['hidden_resource_ids'],
                         [self.audio.pk, self.hidden.pk, self.video.pk])

    def test_attempt_keeps_its_expiry_after_withdrawal(self):
        quiz = linked_quiz('Class quiz', self.course)
        enroll(self.course, self.student)
        attempt = self.data(self.start_api(self.student, quiz.pk), 201)
        self.data(self.call(self.student, 'delete', f'/quiz/classes/{self.course.pk}/enrollment/'))
        replay = self.data(self.start_api(self.student, quiz.pk))
        self.assertEqual((replay['id'], replay['expires_at']), (attempt['id'],
                                                               attempt['expires_at']))
