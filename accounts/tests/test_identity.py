from io import StringIO
from unittest import mock

from django.contrib import admin
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from accounts.forms import CustomUserChangeForm
from accounts.identity import find_identity_collisions, normalize_email, normalize_matric
from .helpers import APITestMixin, User, make_user


class NormalizationTests(TestCase):
    def test_pure_normalizers(self):
        self.assertEqual(normalize_email('  Amina.Y@Example.TEST '), 'amina.y@example.test')
        self.assertEqual(normalize_matric(' csc/21/001 '), 'CSC/21/001')
        self.assertIsNone(normalize_matric('   '))
        self.assertIsNone(normalize_matric(''))
        self.assertIsNone(normalize_matric(None))

    def test_every_save_path_normalizes(self):
        user = make_user(email=' Mixed@Case.Test ', matric_number=' abc/1 ', department='  Physics ')
        user.refresh_from_db()
        self.assertEqual((user.email, user.matric_number, user.department),
                         ('mixed@case.test', 'ABC/1', 'Physics'))
        user.matric_number = '  '
        user.save()
        user.refresh_from_db()
        self.assertIsNone(user.matric_number)

    def test_natural_key_lookup_is_case_insensitive(self):
        user = make_user(email='admin@example.test')
        self.assertEqual(User.objects.get_by_natural_key(' ADMIN@example.test '), user)


class DatabaseConstraintTests(TestCase):
    """Proves the constraints themselves (bypassing model.save normalization)."""

    def test_case_variant_email_rejected_by_database(self):
        make_user(email='one@example.test')
        other = make_user(email='two@example.test')
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=other.pk).update(email='ONE@example.test')

    def test_case_variant_matric_rejected_by_database(self):
        make_user(email='one@example.test', matric_number='CSC/1')
        other = make_user(email='two@example.test')
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=other.pk).update(matric_number='csc/1')

    def test_blank_matric_rejected_but_many_nulls_allowed(self):
        users = [make_user(email=f'u{i}@example.test') for i in range(3)]
        self.assertEqual(User.objects.filter(matric_number__isnull=True).count(), 3)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=users[0].pk).update(matric_number='')

    def test_database_vendor(self):
        # Constraint acceptance is meaningful on the production engine.
        self.assertEqual(connection.vendor, 'postgresql')


class CollisionReportTests(TestCase):
    def test_collision_function_reports_ids_only(self):
        report = find_identity_collisions([
            (1, 'A@x.test', 'csc/1'), (2, 'a@X.test ', 'CSC/1 '), (3, 'b@x.test', ''),
            (4, 'c@x.test', '  '), (5, 'd@x.test', None), (6, 'e@x.test', 'CSC/2')])
        self.assertEqual(report, {'email': [[1, 2]], 'matric_number': [[1, 2]]})

    def test_command_passes_on_clean_data(self):
        make_user(email='one@example.test', matric_number='A/1')
        make_user(email='two@example.test')
        out = StringIO()
        call_command('identity_collision_report', stdout=out)
        self.assertIn('No identity collisions found.', out.getvalue())

    def test_command_passes_before_accounts_table_exists(self):
        out = StringIO()
        with mock.patch.object(connection.introspection, 'table_names', return_value=[]):
            call_command('identity_collision_report', stdout=out)
        self.assertIn('No accounts table yet', out.getvalue())

    def test_command_fails_with_ids_and_no_pii(self):
        # Simulate legacy pre-constraint rows: drop the case-insensitive indexes
        # inside this test's (rolled back) transaction and write raw SQL.
        a = make_user(email='legacy@example.test', matric_number='LEG/1')
        b = make_user(email='other@example.test', matric_number='LEG/2')
        with connection.cursor() as cursor:
            # Expression unique constraints are unique indexes on PostgreSQL.
            cursor.execute('DROP INDEX accounts_user_email_ci_unique')
            cursor.execute('DROP INDEX accounts_user_matric_ci_unique')
            cursor.execute('UPDATE accounts_customuser SET email=%s, matric_number=%s WHERE id=%s',
                           ['LEGACY@example.test', 'leg/1', b.pk])
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command('identity_collision_report', stdout=out)
        output = out.getvalue()
        self.assertIn(f'email: user IDs {a.pk}, {b.pk}', output)
        self.assertIn(f'matric_number: user IDs {a.pk}, {b.pk}', output)
        self.assertNotIn('legacy', output.lower())
        self.assertNotIn('LEG/1', output.upper().replace('USER IDS', ''))
        self.assertEqual(User.objects.count(), 2)


class IdentityMigrationTests(TransactionTestCase):
    """0003 blocks on collisions without changing users and normalizes otherwise."""

    before = [('accounts', '0002_customuser_role')]
    after = [('accounts', '0004_customuser_identity_constraints')]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        try:
            self.migrate(self.after)
        finally:
            super().tearDown()

    def test_blocks_on_collisions_then_normalizes_after_cleanup(self):
        apps = self.migrate(self.before)
        OldUser = apps.get_model('accounts', 'CustomUser')
        first = OldUser.objects.create(email='Dup@Example.test', matric_number=' x/1 ', department=' Maths ')
        second = OldUser.objects.create(email='dup@example.TEST', matric_number='')
        third = OldUser.objects.create(email='third@example.test', matric_number='X/1')

        with self.assertRaisesMessage(Exception, f'email: user IDs {first.pk}, {second.pk}') as caught:
            self.migrate(self.after)
        self.assertIn(f'matric_number: user IDs {first.pk}, {third.pk}', str(caught.exception))
        self.assertNotIn('dup@', str(caught.exception).lower())
        rows = list(OldUser.objects.order_by('pk').values_list('email', 'matric_number'))
        self.assertEqual(rows, [('Dup@Example.test', ' x/1 '), ('dup@example.TEST', ''),
                                ('third@example.test', 'X/1')])  # untouched, nothing merged

        # Lead-approved manual cleanup, then the migration proceeds.
        OldUser.objects.filter(pk=second.pk).update(email='second@example.test')
        OldUser.objects.filter(pk=third.pk).update(matric_number='X/3')
        self.migrate(self.after)
        rows = list(User.objects.order_by('pk').values_list('email', 'matric_number', 'department', 'role'))
        self.assertEqual(rows, [('dup@example.test', 'X/1', 'Maths', 'student'),
                                ('second@example.test', None, '', 'student'),
                                ('third@example.test', 'X/3', '', 'student')])


class AdminFormTests(TestCase):
    def test_admin_shows_role(self):
        model_admin = admin.site._registry[User]
        self.assertIn('role', model_admin.list_display)
        self.assertIn('role', model_admin.list_filter)
        fields = [f for _, opts in model_admin.fieldsets for f in opts['fields']]
        self.assertIn('role', fields)

    def test_change_form_reports_case_insensitive_duplicates(self):
        make_user(email='taken@example.test', matric_number='T/1')
        other = make_user(email='other@example.test')
        form = CustomUserChangeForm(instance=other, data={
            'email': 'TAKEN@example.test', 'first_name': 'A', 'last_name': 'B',
            'matric_number': 't/1', 'department': '', 'level': '', 'role': 'student',
            'is_active': True, 'date_joined': other.date_joined})
        self.assertFalse(form.is_valid())
        self.assertIn('email', form.errors)
        self.assertIn('matric_number', form.errors)


class ProvisioningTests(APITestMixin, TestCase):
    def test_createsuperuser_is_product_admin(self):
        user = User.objects.create_superuser(email='Root@Example.test', password='x-Secure-Pass-1')
        self.assertEqual((user.role, user.is_staff, user.is_superuser, user.email),
                         ('admin', True, True, 'root@example.test'))
        with self.assertRaises(ValueError):
            User.objects.create_superuser(email='bad@example.test', password='x', role='student')

    def test_createsuperuser_command(self):
        call_command('createsuperuser', interactive=False, email='ops@example.test', stdout=StringIO())
        user = User.objects.get(email='ops@example.test')
        self.assertEqual(user.role, 'admin')

    def test_existing_staff_is_not_silently_promoted(self):
        staff = make_user(email='staff@example.test', is_staff=True)
        staff.save()
        staff.refresh_from_db()
        self.assertEqual(staff.role, 'student')

    def test_provision_admin_is_explicit_and_idempotent(self):
        user = make_user(email='lead@example.test')
        out = StringIO()
        call_command('provision_admin', email='LEAD@example.test', stdout=out)
        user.refresh_from_db()
        self.assertEqual((user.role, user.is_staff), ('admin', False))
        self.assertIn('updated role', out.getvalue())
        out = StringIO()
        call_command('provision_admin', user_id=user.pk, staff=True, stdout=out)
        call_command('provision_admin', user_id=user.pk, staff=True, stdout=out)
        user.refresh_from_db()
        self.assertTrue(user.is_staff)
        self.assertIn('already provisioned; no change', out.getvalue())
        # The provisioned admin authenticates with a consistent product role.
        self.assertEqual(self.login('lead@example.test').json()['data']['user']['role'], 'admin')

    def test_provision_admin_refuses_missing_or_inactive(self):
        with self.assertRaises(CommandError):
            call_command('provision_admin', email='missing@example.test', stdout=StringIO())
        inactive = make_user(email='gone@example.test', is_active=False)
        with self.assertRaises(CommandError):
            call_command('provision_admin', user_id=inactive.pk, stdout=StringIO())
        inactive.refresh_from_db()
        self.assertEqual(inactive.role, 'student')

    def test_existing_superusers_reconciliation(self):
        legacy = make_user(email='legacy-root@example.test', is_staff=True, is_superuser=True)
        plain = make_user(email='plain@example.test')
        call_command('provision_admin', existing_superusers=True, stdout=StringIO())
        legacy.refresh_from_db()
        plain.refresh_from_db()
        self.assertEqual((legacy.role, plain.role), ('admin', 'student'))
