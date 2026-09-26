from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from .forms import ImpurityTestForm, OpenCookRunForm
from .models import FireHearth, ImpurityTest, ResinLot
from .services.assays import (
    AssayConflict,
    assert_can_open_with_lot,
    record_assay,
    valid_open_assay,
    void_assay,
)
from .services.roles import (
    SUPERVISOR_GROUP,
    WORKER_GROUP,
    can_record_assay,
    can_void_assay,
    ensure_role_groups,
)

User = get_user_model()


class AssayRuleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.worker = User.objects.create_user("w1", password="x")
        cls.supervisor = User.objects.create_user("s1", password="x")
        groups = ensure_role_groups()
        cls.worker.groups.add(groups[0])  # 值守工
        cls.supervisor.groups.add(groups[1])  # 主管
        cls.lot = ResinLot.objects.create(
            lotCode="L1",
            originPlace="松脂坳",
            arrivalKg=Decimal("100"),
            receivedAt=timezone.now(),
        )
        cls.today = timezone.localdate()

    def _record(self, *, day_offset=0, passed=True, pct="1.5", lot=None):
        return record_assay(
            lot=lot or self.lot,
            sampled_on=self.today - timedelta(days=day_offset),
            impurity_pct=Decimal(pct),
            passed=passed,
            chemist="化验林姐",
            user=self.worker,
        )

    def test_fresh_pass_is_valid(self):
        t = self._record(day_offset=0)
        self.assertEqual(valid_open_assay(self.lot), t)
        self.assertIsNone(assert_can_open_with_lot(self.lot))

    def test_five_day_boundary_valid(self):
        # 边界：抽检日恰为开灶日的五个自然日前 → 仍有效；6 天 → 过期
        edge_lot = ResinLot.objects.create(
            lotCode="EDGE",
            originPlace="桐油坑",
            arrivalKg=Decimal("10"),
            receivedAt=timezone.now(),
        )
        ImpurityTest.objects.create(
            lot=edge_lot,
            sampledOn=self.today - timedelta(days=5),
            impurityPct=Decimal("2"),
            passed=True,
            chemist="x",
        )
        self.assertIsNotNone(valid_open_assay(edge_lot))
        self.assertIsNone(assert_can_open_with_lot(edge_lot))

        stale_lot = ResinLot.objects.create(
            lotCode="STALE",
            originPlace="桐油坑",
            arrivalKg=Decimal("10"),
            receivedAt=timezone.now(),
        )
        ImpurityTest.objects.create(
            lot=stale_lot,
            sampledOn=self.today - timedelta(days=6),
            impurityPct=Decimal("2"),
            passed=True,
            chemist="x",
        )
        self.assertIsNone(valid_open_assay(stale_lot))

    def test_six_days_old_is_stale(self):
        self._record(day_offset=6)
        self.assertIsNone(valid_open_assay(self.lot))
        with self.assertRaises(ValidationError) as ctx:
            assert_can_open_with_lot(self.lot)
        self.assertIn("有效窗", ctx.exception.messages[0])

    def test_failed_latest_not_valid(self):
        self._record(day_offset=0, passed=False)
        self.assertIsNone(valid_open_assay(self.lot))
        with self.assertRaises(ValidationError) as ctx:
            assert_can_open_with_lot(self.lot)
        self.assertIn("不合格", ctx.exception.messages[0])

    def test_no_assay_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            assert_can_open_with_lot(self.lot)
        self.assertIn("尚无有效杂质抽检", ctx.exception.messages[0])

    def test_voided_assay_excluded(self):
        t = self._record(day_offset=0)
        void_assay(t, user=self.supervisor)
        t.refresh_from_db()
        self.assertTrue(t.is_void)
        self.assertIsNone(valid_open_assay(self.lot))

    def test_latest_must_pass_when_older_passed_exists(self):
        # 老的合格，最新一张不合格 → 整批不可开灶
        self._record(day_offset=3, passed=True)
        self._record(day_offset=0, passed=False)
        self.assertIsNone(valid_open_assay(self.lot))

    def test_same_lot_day_conflict_echoes_existing(self):
        t = self._record(day_offset=1)
        with self.assertRaises(AssayConflict) as ctx:
            self._record(day_offset=1)
        self.assertEqual(ctx.exception.existing.pk, t.pk)
        self.assertIn(f"#{t.pk}", ctx.exception.messages[0])
        self.assertEqual(
            ImpurityTest.objects.filter(lot=self.lot, sampledOn=t.sampledOn).count(),
            1,
        )

    def test_same_day_allowed_after_void(self):
        t = self._record(day_offset=2)
        void_assay(t, user=self.supervisor)
        t2 = self._record(day_offset=2)
        self.assertNotEqual(t.pk, t2.pk)
        self.assertEqual(valid_open_assay(self.lot), t2)

    def test_different_lots_same_day_allowed(self):
        other = ResinLot.objects.create(
            lotCode="L2",
            originPlace="桐油坑",
            arrivalKg=Decimal("50"),
            receivedAt=timezone.now(),
        )
        self._record(day_offset=0, lot=self.lot)
        self._record(day_offset=0, lot=other)

    def test_pct_bounds_form(self):
        base = {
            "lot": self.lot.pk,
            "sampledOn": self.today.strftime("%Y-%m-%d"),
            "passed": True,
            "chemist": "化验林姐",
        }
        for bad in ("-0.01", "100.01"):
            form = ImpurityTestForm({**base, "impurityPct": bad})
            self.assertFalse(form.is_valid(), bad)
            self.assertIn("impurityPct", form.errors)
        for good in ("0", "100", "37.5"):
            form = ImpurityTestForm({**base, "impurityPct": good})
            self.assertTrue(form.is_valid(), form.errors)

    def test_roles(self):
        plain = User.objects.create_user("plain", password="x")
        admin = User.objects.create_superuser("root", "r@x", "x")
        self.assertTrue(can_record_assay(self.worker))
        self.assertFalse(can_record_assay(plain))
        self.assertTrue(can_void_assay(self.supervisor))
        self.assertFalse(can_void_assay(self.worker))
        self.assertTrue(can_void_assay(admin))
        self.assertEqual(WORKER_GROUP, "值守工")
        self.assertEqual(SUPERVISOR_GROUP, "主管")


class OpenRunGateViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.worker = User.objects.create_user("w2", password="x")
        cls.worker.groups.add(ensure_role_groups()[0])
        cls.hearth = FireHearth.objects.create(
            lane=9, tag="试火", resinGrade="一级脂"
        )
        cls.lot_ok = ResinLot.objects.create(
            lotCode="OK1",
            originPlace="松脂坳东沟",
            arrivalKg=Decimal("100"),
            receivedAt=timezone.now(),
        )
        cls.lot_bare = ResinLot.objects.create(
            lotCode="BARE1",
            originPlace="松脂坳西岔",
            arrivalKg=Decimal("100"),
            receivedAt=timezone.now(),
        )
        ImpurityTest.objects.create(
            lot=cls.lot_ok,
            sampledOn=timezone.localdate(),
            impurityPct=Decimal("1.2"),
            passed=True,
            chemist="化验林姐",
        )

    def test_open_run_form_gates_lot_without_assay(self):
        form = OpenCookRunForm(
            {
                "resinLot": self.lot_bare.pk,
                "openedAt": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "targetSoftPointC": "88",
            },
            hearth=self.hearth,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("resinLot", form.errors)
        self.assertIn("尚无有效杂质抽检", form.errors["resinLot"][0])

    def test_open_run_view_cannot_bypass_gate(self):
        self.client.force_login(self.worker)
        resp = self.client.post(
            f"/hearth/{self.hearth.pk}/open-run/",
            {
                "resinLot": self.lot_bare.pk,
                "openedAt": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "targetSoftPointC": "88",
            },
        )
        self.assertIn(resp.status_code, (200, 302))
        self.assertIsNone(self.hearth.open_run())

    def test_open_run_succeeds_with_valid_assay(self):
        self.client.force_login(self.worker)
        resp = self.client.post(
            f"/hearth/{self.hearth.pk}/open-run/",
            {
                "resinLot": self.lot_ok.pk,
                "openedAt": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "targetSoftPointC": "88",
            },
        )
        self.assertIn(resp.status_code, (200, 302))
        run = self.hearth.open_run()
        self.assertIsNotNone(run)
        self.assertEqual(run.resinLot_id, self.lot_ok.pk)


class AssayViewPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        groups = ensure_role_groups()
        cls.worker = User.objects.create_user("w3", password="x")
        cls.worker.groups.add(groups[0])
        cls.supervisor = User.objects.create_user("s3", password="x")
        cls.supervisor.groups.add(groups[1])
        cls.plain = User.objects.create_user("p3", password="x")
        cls.lot = ResinLot.objects.create(
            lotCode="P1",
            originPlace="松脂坳",
            arrivalKg=Decimal("10"),
            receivedAt=timezone.now(),
        )

    def test_worker_can_record_plain_cannot(self):
        self.client.force_login(self.plain)
        resp = self.client.post(
            "/assays/",
            {
                "lot": self.lot.pk,
                "sampledOn": timezone.localdate().strftime("%Y-%m-%d"),
                "impurityPct": "1.5",
                "passed": True,
                "chemist": "化验林姐",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ImpurityTest.objects.count(), 0)

        self.client.force_login(self.worker)
        resp = self.client.post(
            "/assays/",
            {
                "lot": self.lot.pk,
                "sampledOn": timezone.localdate().strftime("%Y-%m-%d"),
                "impurityPct": "1.5",
                "passed": True,
                "chemist": "化验林姐",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ImpurityTest.objects.count(), 1)

    def test_only_supervisor_can_void(self):
        t = ImpurityTest.objects.create(
            lot=self.lot,
            sampledOn=timezone.localdate(),
            impurityPct=Decimal("1"),
            passed=True,
            chemist="x",
        )
        self.client.force_login(self.worker)
        resp = self.client.post(f"/assays/{t.pk}/void/")
        self.assertEqual(resp.status_code, 302)
        t.refresh_from_db()
        self.assertIsNone(t.voidedAt)

        self.client.force_login(self.supervisor)
        resp = self.client.post(f"/assays/{t.pk}/void/")
        self.assertEqual(resp.status_code, 302)
        t.refresh_from_db()
        self.assertIsNotNone(t.voidedAt)
        self.assertEqual(t.voidedBy, self.supervisor)
