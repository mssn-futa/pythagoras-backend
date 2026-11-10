import threading

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from accounts import student_admin

from .helpers import PASSWORD, APITestMixin, make_user

User = get_user_model()
LIST = "/accounts/students/"


def detail(pk):
    return f"/accounts/students/{pk}/"


class StudentAdminBase(APITestMixin):
    def setUp(self):
        super().setUp()
        self.admin = make_user("admin@example.test", role="admin", first_name="Ada", last_name="Admin")
        self.other_admin = make_user("admin2@example.test", role="admin", first_name="Bo", last_name="Admin")

    def as_admin(self, email="admin@example.test"):
        access = self.tokens(email)["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def student(self, n, **fields):
        fields.setdefault("first_name", f"Stu{n}")
        fields.setdefault("last_name", "Student")
        fields.setdefault("matric_number", f"CSC/21/{n:03d}")
        return make_user(f"s{n}@example.test", **fields)


class PermissionMatrixTests(StudentAdminBase, TestCase):
    def test_matrix(self):
        student = self.student(1)
        inactive_admin = make_user("old@example.test", role="admin", is_active=False)
        urls = [("get", LIST), ("get", detail(student.pk)), ("patch", detail(student.pk))]

        for method, url in urls:  # anonymous
            response = getattr(self.client, method)(url, {}, format="json")
            self.assertEqual(response.status_code, 401, (method, url))

        access = self.tokens("s1@example.test")["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        for method, url in urls:  # student
            response = getattr(self.client, method)(url, {}, format="json")
            self.assertEqual(response.status_code, 403, (method, url))
            self.assertEqual(response.json()["data"]["code"], "PERMISSION_DENIED")

        # An admin token whose account is deactivated afterwards.
        self.client.credentials()
        inactive_admin.is_active = True
        inactive_admin.save()
        access = self.tokens("old@example.test")["access"]
        inactive_admin.is_active = False
        inactive_admin.save()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        for method, url in urls:
            response = getattr(self.client, method)(url, {}, format="json")
            self.assertEqual(response.status_code, 403, (method, url))
            self.assertEqual(response.json()["data"]["code"], "ACCOUNT_INACTIVE")

        self.as_admin()
        self.assertEqual(self.client.get(LIST).status_code, 200)
        self.assertEqual(self.client.get(detail(student.pk)).status_code, 200)

    def test_put_and_delete_not_allowed(self):
        student = self.student(1)
        self.as_admin()
        self.assertEqual(self.client.put(detail(student.pk), {}, format="json").status_code, 405)
        self.assertEqual(self.client.delete(detail(student.pk)).status_code, 405)
        self.assertEqual(self.client.post(LIST, {}, format="json").status_code, 405)


class StudentListTests(StudentAdminBase, TestCase):
    def setUp(self):
        super().setUp()
        self.as_admin()

    def results(self, query=""):
        response = self.client.get(f"{LIST}{query}")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["success"])
        return body["data"]

    def test_only_students_listed_with_shape(self):
        self.student(1, department="Computer Science", level="300")
        data = self.results()
        self.assertEqual(data["count"], 1)
        row = data["results"][0]
        self.assertEqual(
            set(row),
            {"id", "email", "first_name", "last_name", "matric_number", "department", "level",
             "role", "is_active", "date_joined"},
        )
        self.assertEqual(row["role"], "student")
        self.assertTrue(row["date_joined"].endswith("Z"))
        self.assertNotIn("password", row)

    def test_search_name_email_matric(self):
        self.student(1, first_name="Amina", last_name="Yusuf")
        self.student(2, first_name="Bola", last_name="Okoye", matric_number="EEE/20/045")
        self.assertEqual([r["first_name"] for r in self.results("?search=amina")["results"]], ["Amina"])
        self.assertEqual([r["first_name"] for r in self.results("?search=amina%20yusuf")["results"]], ["Amina"])
        self.assertEqual([r["first_name"] for r in self.results("?search=eee/20")["results"]], ["Bola"])
        self.assertEqual([r["first_name"] for r in self.results("?search=S2@EXAMPLE")["results"]], ["Bola"])
        self.assertEqual(self.results("?search=nobody")["count"], 0)
        self.assertEqual(self.results("?search=admin")["count"], 0)  # admins never match

    def test_filters(self):
        self.student(1, department="Computer Science", level="300")
        self.student(2, department="Computer Science", level="400", is_active=False)
        self.student(3, department="Physics", level="300")
        ids = lambda q: sorted(r["email"] for r in self.results(q)["results"])
        self.assertEqual(ids("?department=%20computer%20SCIENCE%20"), ["s1@example.test", "s2@example.test"])
        self.assertEqual(ids("?level=300"), ["s1@example.test", "s3@example.test"])
        self.assertEqual(ids("?is_active=false"), ["s2@example.test"])
        self.assertEqual(ids("?is_active=true"), ["s1@example.test", "s3@example.test"])
        self.assertEqual(ids("?department=computer science&level=300"), ["s1@example.test"])
        self.assertEqual(ids("?department=physics&level=300&is_active=true&search=stu3"), ["s3@example.test"])
        self.assertEqual(ids("?level=&is_active="), ["s1@example.test", "s2@example.test", "s3@example.test"])

    def test_invalid_filters_are_field_errors(self):
        for query, field in (("?level=900", "level"), ("?is_active=maybe", "is_active")):
            response = self.client.get(LIST + query)
            self.assertEqual(response.status_code, 400)
            self.assertIn(field, response.json()["data"]["fields"])

    def test_pagination_and_ordering(self):
        for n in range(1, 8):  # identical names force the id tiebreak
            self.student(n, first_name="Same", last_name="Name")
        page1 = self.results("?page_size=3")
        self.assertEqual((page1["count"], len(page1["results"])), (7, 3))
        self.assertIsNotNone(page1["next"])
        self.assertIsNone(page1["previous"])
        seen = [r["id"] for r in page1["results"]]
        seen += [r["id"] for r in self.results("?page_size=3&page=2")["results"]]
        last = self.results("?page_size=3&page=3")
        self.assertEqual(len(last["results"]), 1)
        self.assertIsNone(last["next"])
        seen += [r["id"] for r in last["results"]]
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(set(seen)), 7)
        self.assertEqual(len(self.results()["results"]), 7)
        self.assertEqual(self.client.get(LIST + "?page=9").status_code, 404)

    def test_page_size_capped_and_count_covers_filtered_set(self):
        for n in range(1, 4):
            self.student(n, level="100" if n < 3 else "200")
        data = self.results("?level=100&page_size=1000")
        self.assertEqual(data["count"], 2)

    def test_default_order_by_name(self):
        self.student(1, first_name="Zed", last_name="Alpha")
        self.student(2, first_name="Amy", last_name="Beta")
        self.student(3, first_name="Abe", last_name="Beta")
        self.assertEqual([r["first_name"] for r in self.results()["results"]], ["Zed", "Abe", "Amy"])


class StudentDetailTests(StudentAdminBase, TestCase):
    def setUp(self):
        super().setUp()
        self.target = self.student(1, department="Computer Science", level="300")
        self.as_admin()

    def patch(self, payload, pk=None):
        return self.client.patch(detail(pk or self.target.pk), payload, format="json")

    def errors(self, response):
        self.assertEqual(response.status_code, 400, response.content)
        return response.json()["data"]["fields"]

    def test_detail(self):
        response = self.client.get(detail(self.target.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["email"], "s1@example.test")

    def test_unknown_and_admin_ids_are_404(self):
        for pk in (999999, self.admin.pk, self.other_admin.pk):
            for response in (self.client.get(detail(pk)), self.patch({"first_name": "X"}, pk)):
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["data"]["code"], "NOT_FOUND")
        self.other_admin.refresh_from_db()
        self.assertEqual(self.other_admin.first_name, "Bo")
        self.assertEqual(self.patch({"is_active": False}, self.other_admin.pk).status_code, 404)
        self.other_admin.refresh_from_db()
        self.assertTrue(self.other_admin.is_active)

    def test_allowed_fields_persist_and_normalize(self):
        response = self.patch({
            "first_name": "Aminah", "last_name": "Yusuf-Bello", "department": "  Physics ",
            "level": "400", "email": "  New.Mail@Example.TEST ", "matric_number": " phy/21/009 ",
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["message"], "Student updated.")
        self.target.refresh_from_db()
        self.assertEqual(
            (self.target.first_name, self.target.last_name, self.target.department, self.target.level,
             self.target.email, self.target.matric_number),
            ("Aminah", "Yusuf-Bello", "Physics", "400", "new.mail@example.test", "PHY/21/009"))
        self.assertEqual(self.client.get(detail(self.target.pk)).json()["data"]["level"], "400")

    def test_blank_matric_becomes_null_and_blank_level_allowed(self):
        response = self.patch({"matric_number": "   ", "level": ""})
        self.assertEqual(response.status_code, 200, response.content)
        self.target.refresh_from_db()
        self.assertIsNone(self.target.matric_number)
        self.assertEqual(self.target.level, "")
        self.assertEqual(self.patch({"matric_number": None}).status_code, 200)

    def test_own_values_do_not_collide_with_self(self):
        self.assertEqual(self.patch({"email": "S1@Example.test", "matric_number": "csc/21/001"}).status_code, 200)

    def test_protected_fields_rejected_and_nothing_applied(self):
        for key in ("role", "is_staff", "is_superuser", "groups", "user_permissions", "password",
                    "id", "date_joined", "last_login", "bogus"):
            errors = self.errors(self.patch({"first_name": "Changed", key: "admin"}))
            self.assertEqual(errors, {key: ["This field cannot be set or changed here."]}, key)
        self.target.refresh_from_db()
        self.assertEqual((self.target.first_name, self.target.role), ("Stu1", "student"))
        self.assertFalse(self.target.is_staff)

    def test_validation_errors(self):
        self.assertIn("level", self.errors(self.patch({"level": "900"})))
        self.assertIn("first_name", self.errors(self.patch({"first_name": "  "})))
        self.assertIn("email", self.errors(self.patch({"email": "not-an-email"})))
        self.assertIn("email", self.errors(self.patch({"email": ""})))

    def test_normalized_collisions_are_field_errors(self):
        self.student(2)
        make_user("adm.mail@example.test", role="admin", matric_number="ADM/1")
        for payload, field in (
            ({"email": "S2@EXAMPLE.TEST"}, "email"),
            ({"email": " Adm.Mail@example.test"}, "email"),
            ({"matric_number": "csc/21/002"}, "matric_number"),
            ({"matric_number": " adm/1 "}, "matric_number"),
        ):
            self.assertIn(field, self.errors(self.patch(payload)), payload)
        self.target.refresh_from_db()
        self.assertEqual((self.target.email, self.target.matric_number), ("s1@example.test", "CSC/21/001"))

    def test_deactivate_blocks_existing_access_and_refresh_then_reactivate(self):
        student_client = APIClient()
        login = student_client.post("/accounts/login/", {"email_or_matric": "s1@example.test",
                                                         "password": PASSWORD}, format="json")
        tokens = login.json()["data"]["tokens"]
        student_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        self.assertEqual(student_client.get("/accounts/me/").status_code, 200)

        response = self.patch({"is_active": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["data"]["is_active"])
        denied = student_client.get("/accounts/me/")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["data"]["code"], "ACCOUNT_INACTIVE")
        refresh = APIClient().post("/accounts/refresh/", {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(refresh.status_code, 403)
        self.assertEqual(refresh.json()["data"]["code"], "ACCOUNT_INACTIVE")
        self.assertEqual(self.login("s1@example.test").status_code, 403)
        self.assertEqual(self.client.get(LIST + "?is_active=false").json()["data"]["count"], 1)

        self.assertEqual(self.patch({"is_active": True}).status_code, 200)
        self.assertEqual(student_client.get("/accounts/me/").status_code, 200)
        self.assertEqual(self.login("s1@example.test").status_code, 200)

    def test_is_active_requires_boolean(self):
        self.assertIn("is_active", self.errors(self.patch({"is_active": "maybe"})))

    def test_empty_patch_is_a_noop(self):
        self.assertEqual(self.patch({}).status_code, 200)


class StatusProtectionHelperTests(StudentAdminBase, TestCase):
    def test_self_deactivation_refused(self):
        from pythagoras.utils import APIError
        with self.assertRaises(APIError) as ctx:
            student_admin.set_account_active(self.admin.pk, False, actor=self.admin)
        self.assertEqual(ctx.exception.code, "SELF_DEACTIVATION")
        self.assertEqual(ctx.exception.status_code, 409)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_last_active_admin_refused_but_not_when_another_remains(self):
        from pythagoras.utils import APIError
        self.assertEqual(student_admin.set_account_active(self.other_admin.pk, False, actor=self.admin).is_active, False)
        make_user("sup@example.test", role="admin", is_active=False)
        with self.assertRaises(APIError) as ctx:
            student_admin.set_account_active(self.admin.pk, False, actor=None)
        self.assertEqual(ctx.exception.code, "LAST_ADMIN")
        self.assertIsNone(student_admin.set_account_active(999999, False))
        # Reactivation is always allowed.
        self.assertTrue(student_admin.set_account_active(self.other_admin.pk, True, actor=self.admin).is_active)


class StatusConcurrencyTests(StudentAdminBase, TransactionTestCase):
    """Real threads on PostgreSQL: two admins deactivating each other (or one
    admin deactivating itself) can never leave zero active administrators."""

    workers = 6

    def run_threads(self, jobs):
        from pythagoras.utils import APIError
        barrier = threading.Barrier(len(jobs))
        outcomes, lock = [], threading.Lock()

        def run(job):
            try:
                barrier.wait(10)
                try:
                    job()
                    result = "ok"
                except APIError as exc:
                    result = exc.code
                with lock:
                    outcomes.append(result)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(job,)) for job in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(len(outcomes), len(jobs))
        return outcomes

    def test_mutual_deactivation_keeps_one_admin(self):
        self.assertEqual(connection.vendor, "postgresql")
        a, b = self.admin, self.other_admin
        for _ in range(5):
            User.objects.filter(pk__in=[a.pk, b.pk]).update(is_active=True)
            outcomes = self.run_threads([
                lambda: student_admin.set_account_active(b.pk, False, actor=a),
                lambda: student_admin.set_account_active(a.pk, False, actor=b),
            ])
            self.assertEqual(sorted(outcomes), ["LAST_ADMIN", "ok"], outcomes)
            self.assertEqual(User.objects.filter(role="admin", is_active=True).count(), 1)

    def test_many_actors_deactivating_many_admins_never_reaches_zero(self):
        extra = [make_user(f"x{i}@example.test", role="admin") for i in range(3)]
        admins = [self.admin, self.other_admin, *extra]
        # An external actor (None) deactivates every admin at once.
        outcomes = self.run_threads([
            (lambda pk=a.pk: student_admin.set_account_active(pk, False)) for a in admins
        ])
        self.assertEqual(outcomes.count("ok"), len(admins) - 1, outcomes)
        self.assertEqual(outcomes.count("LAST_ADMIN"), 1)
        self.assertEqual(User.objects.filter(role="admin", is_active=True).count(), 1)

    def test_self_deactivation_under_concurrency(self):
        outcomes = self.run_threads([
            lambda: student_admin.set_account_active(self.admin.pk, False, actor=self.admin)
            for _ in range(self.workers)
        ])
        self.assertEqual(outcomes, ["SELF_DEACTIVATION"] * self.workers)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_concurrent_student_status_patches_via_api_stay_consistent(self):
        from django.db.models.signals import post_save

        student = self.student(1)
        access = self.tokens("admin@example.test")["access"]
        saves, saves_lock, results = [], threading.Lock(), []

        def record(sender, instance, **kwargs):
            # Runs inside the PATCH transaction while the student row is locked,
            # so this order is exactly the commit order of the writers.
            if instance.pk == student.pk:
                with saves_lock:
                    saves.append((instance.last_name, instance.is_active))

        post_save.connect(record, sender=User)
        self.addCleanup(post_save.disconnect, record, sender=User)

        def patch(i):
            def job():
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
                response = client.patch(
                    detail(student.pk), {"is_active": i % 2 == 0, "last_name": f"W{i}"},
                    format="json")
                results.append(response.status_code)
            return job

        self.run_threads([patch(i) for i in range(self.workers)])
        self.assertEqual(results, [200] * self.workers)
        self.assertEqual(len(saves), self.workers)
        self.assertEqual(sorted(name for name, _ in saves), sorted(f"W{i}" for i in range(self.workers)))
        student.refresh_from_db()
        # No lost or torn update: the stored row is exactly the last committed write.
        self.assertEqual((student.last_name, student.is_active), saves[-1])
        self.assertEqual(User.objects.filter(role="admin", is_active=True).count(), 2)

    def test_concurrent_identity_collision_is_field_error_not_500(self):
        s1, s2 = self.student(1), self.student(2)
        access = self.tokens("admin@example.test")["access"]
        statuses, lock = [], threading.Lock()
        barrier = threading.Barrier(2)

        def job(pk):
            try:
                client = APIClient(raise_request_exception=False)
                client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
                barrier.wait(10)
                response = client.patch(detail(pk), {"email": "same@example.test"}, format="json")
                with lock:
                    statuses.append(response.status_code)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=job, args=(pk,)) for pk in (s1.pk, s2.pk)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(statuses), [200, 400])


class DjangoAdminRetentionTests(StudentAdminBase, TestCase):
    """The Django admin change view uses the same admin-retention protection."""

    def setUp(self):
        super().setUp()
        self.admin.is_staff = self.admin.is_superuser = True
        self.admin.save()
        self.client.force_login(self.admin)

    def post_change(self, user, **overrides):
        from django import forms as dj_forms
        url = f"/admin/accounts/customuser/{user.pk}/change/"
        form = self.client.get(url).context["adminform"].form
        data = {}
        for name, field in form.fields.items():
            value = form.initial.get(name)
            if isinstance(field, dj_forms.SplitDateTimeField):
                data[f"{name}_0"] = value.strftime("%Y-%m-%d") if value else ""
                data[f"{name}_1"] = value.strftime("%H:%M:%S") if value else ""
            elif isinstance(field, dj_forms.ModelMultipleChoiceField):
                data[name] = [obj.pk for obj in value or []]
            elif isinstance(field, dj_forms.BooleanField):
                if value:
                    data[name] = "on"
            elif value is not None:
                data[name] = value
        for key, value in overrides.items():
            if value is False:
                data.pop(key, None)
            else:
                data[key] = "on" if value is True else value
        return self.client.post(url, data)

    def assert_refused(self, response, user, message):
        self.assertEqual(response.status_code, 200, "form is redisplayed with an error")
        self.assertContains(response, message)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertEqual(user.role, "admin")

    def test_self_deactivation_and_self_demotion_refused(self):
        self.assert_refused(self.post_change(self.admin, is_active=False), self.admin,
                            "cannot deactivate your own account")
        self.assert_refused(self.post_change(self.admin, role="student"), self.admin,
                            "cannot remove your own administrator role")

    def test_last_active_admin_cannot_be_deactivated_or_demoted(self):
        self.other_admin.is_active = False
        self.other_admin.save()
        other = make_user("peer@example.test", role="admin")
        # Deactivate/demote the only other active admin while self remains: allowed once...
        ok = self.post_change(other, is_active=False)
        self.assertEqual(ok.status_code, 302)
        # ...but now `admin` is the last active admin; a different (inactive) staff
        # superuser acting on it is refused too.
        actor = make_user("root@example.test", role="student", is_staff=True, is_superuser=True)
        self.client.force_login(actor)
        self.assert_refused(self.post_change(self.admin, is_active=False), self.admin,
                            "last active administrator")
        self.assert_refused(self.post_change(self.admin, role="student"), self.admin,
                            "last active administrator")

    def test_allowed_changes_still_work(self):
        response = self.post_change(self.other_admin, role="student")
        self.assertEqual(response.status_code, 302)
        self.other_admin.refresh_from_db()
        self.assertEqual(self.other_admin.role, "student")
        student = self.student(1)
        self.assertEqual(self.post_change(student, is_active=False).status_code, 302)
        student.refresh_from_db()
        self.assertFalse(student.is_active)
        self.assertEqual(self.post_change(student, first_name="Renamed").status_code, 302)
        self.assertEqual(self.post_change(self.admin, first_name="Ada2").status_code, 302)


class DjangoAdminDeletionTests(StudentAdminBase, TestCase):
    def setUp(self):
        super().setUp()
        self.admin.is_staff = self.admin.is_superuser = True
        self.admin.save()
        self.client.force_login(self.admin)

    def delete_single(self, user):
        return self.client.post(f"/admin/accounts/customuser/{user.pk}/delete/", {"post": "yes"},
                                follow=True)

    def bulk(self, *users):
        return self.client.post(
            "/admin/accounts/customuser/",
            {"action": "delete_selected", "_selected_action": [u.pk for u in users], "post": "yes"},
            follow=True)

    def messages(self, response):
        return [str(m) for m in response.context["messages"]]

    def test_single_delete_self_refused(self):
        response = self.delete_single(self.admin)
        self.assertIn("You cannot delete your own account.", self.messages(response))
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_single_delete_last_active_admin_refused(self):
        self.other_admin.is_active = False
        self.other_admin.save()
        actor = make_user("root@example.test", role="student", is_staff=True, is_superuser=True)
        self.client.force_login(actor)
        response = self.delete_single(self.admin)
        self.assertTrue(any("last active administrator" in m for m in self.messages(response)))
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_single_delete_allowed_cases(self):
        student = self.student(1)
        self.delete_single(student)
        self.delete_single(self.other_admin)
        self.assertFalse(User.objects.filter(pk__in=[student.pk, self.other_admin.pk]).exists())

    def test_bulk_refuses_whole_set_if_any_violates(self):
        student = self.student(1)
        response = self.bulk(student, self.admin)  # includes self
        self.assertTrue(any("cannot delete your own" in m for m in self.messages(response)))
        self.assertEqual(User.objects.filter(pk__in=[student.pk, self.admin.pk]).count(), 2)

        actor = make_user("root@example.test", role="student", is_staff=True, is_superuser=True)
        self.client.force_login(actor)
        response = self.bulk(student, self.admin, self.other_admin)  # every active admin
        self.assertTrue(any("last active administrator" in m for m in self.messages(response)))
        self.assertEqual(User.objects.filter(pk__in=[student.pk, self.admin.pk, self.other_admin.pk]).count(), 3)

    def test_bulk_allowed_when_an_admin_remains(self):
        students = [self.student(1), self.student(2)]
        self.bulk(*students, self.other_admin)
        self.assertEqual(User.objects.filter(pk__in=[s.pk for s in students]).count(), 0)
        self.assertFalse(User.objects.filter(pk=self.other_admin.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_bulk_confirmation_page_refuses_early(self):
        response = self.client.post(
            "/admin/accounts/customuser/",
            {"action": "delete_selected", "_selected_action": [self.admin.pk]}, follow=True)
        self.assertIn("You cannot delete your own account. Nothing was deleted.", self.messages(response))
