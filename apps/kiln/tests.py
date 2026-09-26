"""杂质抽检（脂检）与开灶门禁测试。"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.kiln.forms import ImpurityAssayForm, OpenCookRunForm
from apps.kiln.models import (
    CookRun,
    FireHearth,
    ImpurityAssay,
    ResinLot,
)
from apps.kiln.services.assay_rules import (
    ASSAY_VALID_DAYS,
    assert_lot_openable,
    assay_status_map,
    is_assay_valid,
    latest_active_assay,
)

User = get_user_model()


class AssayRuleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.worker = User.objects.create_user("worker", password="x")
        cls.sup = User.objects.create_superuser("admin", password="x")
        cls.lot = ResinLot.objects.create(
            lotCode="脂-TEST-1",
            originPlace="测试沟",
            arrivalKg=Decimal("100"),
            receivedAt=timezone.now(),
        )
        cls.today = timezone.localdate()

    def _assay(self, *, on=None, passed=True, pct="1.5", void=False):
        return ImpurityAssay.objects.create(
            lot=self.lot,
            sampledOn=self.today if on is None else on,
            impurityPct=Decimal(pct),
            passed=passed,
            chemistName="化验员",
            createdBy=self.worker,
            voidedAt=timezone.now() if void else None,
            voidedBy=self.sup if void else None,
        )

    def test_passing_today_is_valid(self):
        a = self._assay(on=self.today)
        self.assertTrue(is_assay_valid(latest_active_assay(self.lot)))
        self.assertEqual(assert_lot_openable(self.lot), a)

    def test_five_day_boundary_inclusive(self):
        edge = self._assay(on=self.today - timedelta(days=ASSAY_VALID_DAYS))
        self.assertTrue(is_assay_valid(edge))
        assert_lot_openable(self.lot)  # 不抛异常

    def test_six_days_out_of_window(self):
        stale = self._assay(on=self.today - timedelta(days=ASSAY_VALID_DAYS + 1))
        self.assertFalse(is_assay_valid(stale))
        with self.assertRaises(ValidationError) as ctx:
            assert_lot_openable(self.lot)
        self.assertIn("有效窗", str(ctx.exception))

    def test_no_assay_rejected_chinese(self):
        with self.assertRaises(ValidationError) as ctx:
            assert_lot_openable(self.lot)
        msg = str(ctx.exception)
        self.assertIn("尚无杂质抽检", msg)

    def test_latest_failed_invalidates_even_with_older_pass(self):
        self._assay(on=self.today - timedelta(days=3), passed=True)
        self._assay(on=self.today, passed=False, pct="8.0")
        with self.assertRaises(ValidationError) as ctx:
            assert_lot_openable(self.lot)
        self.assertIn("未通过", str(ctx.exception))

    def test_voided_assay_not_counted(self):
        self._assay(on=self.today, passed=True, void=True)
        # 唯一有效的一张被作废 → 等于无有效检
        with self.assertRaises(ValidationError):
            assert_lot_openable(self.lot)
        self._assay(on=self.today - timedelta(days=2), passed=True)
        # 作废当日检后，两日前的通过检成为最新有效检
        assert_lot_openable(self.lot)

    def test_void_method_is_idempotent(self):
        a = self._assay(on=self.today)
        a.void(self.sup)
        first = a.voidedAt
        a.void(self.worker)  # 重复作废不改写、不换作废人
        a.refresh_from_db()
        self.assertEqual(a.voidedBy, self.sup)
        self.assertEqual(a.voidedAt, first)


class AssayFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.worker = User.objects.create_user("worker", password="x")
        cls.lot = ResinLot.objects.create(
            lotCode="脂-TEST-2",
            originPlace="测试坡",
            arrivalKg=Decimal("100"),
            receivedAt=timezone.now(),
        )
        cls.today = timezone.localdate()

    def _post(self, **overrides):
        data = {
            "lot": self.lot.pk,
            "sampledOn": self.today.isoformat(),
            "impurityPct": "1.20",
            "passed": "on",
            "chemistName": "化验林姐",
        }
        data.update(overrides)
        return ImpurityAssayForm(data)

    def test_pct_bounds(self):
        for bad in ("-0.01", "100.01"):
            self.assertFalse(self._post(impurityPct=bad).is_valid(), bad)

    def test_pct_endpoints_ok(self):
        for good in ("0", "100"):
            self.assertTrue(self._post(impurityPct=good).is_valid(), good)

    def test_same_day_duplicate_rejected_with_existing_tag(self):
        existing = ImpurityAssay.objects.create(
            lot=self.lot,
            sampledOn=self.today,
            impurityPct=Decimal("1.0"),
            passed=True,
            chemistName="化验老吴",
            createdBy=self.worker,
        )
        form = self._post(chemistName="化验林姐")
        self.assertFalse(form.is_valid())
        msg = form.errors.as_text()
        self.assertIn(str(existing), msg)  # 回显已有标识
        self.assertIn("同日不得重复登记", msg)

    def test_same_day_allowed_after_void(self):
        a = ImpurityAssay.objects.create(
            lot=self.lot,
            sampledOn=self.today,
            impurityPct=Decimal("1.0"),
            passed=False,
            chemistName="化验老吴",
            createdBy=self.worker,
        )
        a.void(self.worker)
        self.assertTrue(self._post().is_valid())

    def test_db_constraint_blocks_duplicate_active(self):
        ImpurityAssay.objects.create(
            lot=self.lot,
            sampledOn=self.today,
            impurityPct=Decimal("1.0"),
            passed=True,
            chemistName="甲",
            createdBy=self.worker,
        )
        with transaction.atomic(), self.assertRaises(IntegrityError):
            ImpurityAssay.objects.create(
                lot=self.lot,
                sampledOn=self.today,
                impurityPct=Decimal("2.0"),
                passed=True,
                chemistName="乙",
                createdBy=self.worker,
            )


class OpenRunGateViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.worker = User.objects.create_user("worker", password="x")
        cls.sup = User.objects.create_superuser("admin", password="x")
        cls.lot_ok = ResinLot.objects.create(
            lotCode="脂-OK-1", originPlace="好沟",
            arrivalKg=Decimal("100"), receivedAt=timezone.now(),
        )
        cls.lot_bad = ResinLot.objects.create(
            lotCode="脂-BAD-1", originPlace="坏沟",
            arrivalKg=Decimal("100"), receivedAt=timezone.now(),
        )
        cls.today = timezone.localdate()
        ImpurityAssay.objects.create(
            lot=cls.lot_ok, sampledOn=cls.today, impurityPct=Decimal("1.0"),
            passed=True, chemistName="化验", createdBy=cls.worker,
        )
        ImpurityAssay.objects.create(
            lot=cls.lot_bad, sampledOn=cls.today, impurityPct=Decimal("9.0"),
            passed=False, chemistName="化验", createdBy=cls.worker,
        )
        cls.hearth = FireHearth.objects.create(
            lane=9, tag="测试灶", resinGrade="一级脂",
            phase=FireHearth.PHASE_COLD,
        )

    def _open_payload(self, lot):
        return {
            "resinLot": lot.pk,
            "openedAt": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "targetSoftPointC": "88.00",
        }

    def test_open_run_without_valid_assay_fails(self):
        self.client.force_login(self.worker)
        resp = self.client.post(
            f"/hearth/{self.hearth.pk}/open-run/", self._open_payload(self.lot_bad)
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(CookRun.objects.count(), 0)  # 开灶绕过抽检即失败
        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_COLD)

    def test_open_run_with_valid_assay_succeeds(self):
        self.client.force_login(self.worker)
        resp = self.client.post(
            f"/hearth/{self.hearth.pk}/open-run/", self._open_payload(self.lot_ok)
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(CookRun.objects.count(), 1)
        self.hearth.refresh_from_db()
        self.assertEqual(self.hearth.phase, FireHearth.PHASE_CHARGING)

    def test_open_form_marks_lots_same_source(self):
        form = OpenCookRunForm(hearth=self.hearth)
        labels = dict(form["resinLot"].field.choices)
        self.assertIn("有效检", labels[self.lot_ok.pk])
        self.assertIn("无有效检", labels[self.lot_bad.pk])

    def test_status_map_consistent_with_gate(self):
        m = assay_status_map([self.lot_ok, self.lot_bad])
        self.assertTrue(m[self.lot_ok.pk][1])
        self.assertFalse(m[self.lot_bad.pk][1])


class AssayPermissionViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.worker = User.objects.create_user("worker", password="x")
        cls.sup = User.objects.create_superuser("admin", password="x")
        cls.lot = ResinLot.objects.create(
            lotCode="脂-PERM-1", originPlace="权限沟",
            arrivalKg=Decimal("100"), receivedAt=timezone.now(),
        )

    def _assay(self, passed=True):
        return ImpurityAssay.objects.create(
            lot=self.lot, sampledOn=timezone.localdate(),
            impurityPct=Decimal("1.0"), passed=passed,
            chemistName="化验", createdBy=self.worker,
        )

    def test_worker_can_create_assay(self):
        self.client.force_login(self.worker)
        resp = self.client.post(
            "/assays/",
            {
                "lot": self.lot.pk,
                "sampledOn": timezone.localdate().isoformat(),
                "impurityPct": "1.5",
                "passed": "on",
                "chemistName": "化验新人",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.lot.assays.count(), 1)
        self.assertEqual(self.lot.assays.first().createdBy, self.worker)

    def test_worker_cannot_void(self):
        a = self._assay()
        self.client.force_login(self.worker)
        resp = self.client.post(f"/assays/{a.pk}/void/")
        self.assertEqual(resp.status_code, 302)
        a.refresh_from_db()
        self.assertIsNone(a.voidedAt)

    def test_supervisor_can_void(self):
        a = self._assay()
        self.client.force_login(self.sup)
        resp = self.client.post(f"/assays/{a.pk}/void/")
        self.assertEqual(resp.status_code, 302)
        a.refresh_from_db()
        self.assertIsNotNone(a.voidedAt)
        self.assertEqual(a.voidedBy, self.sup)
        self.assertFalse(is_assay_valid(latest_active_assay(self.lot)))

    def test_feed_pages_render(self):
        self.client.force_login(self.worker)
        self.assertEqual(self.client.get("/assays/").status_code, 200)
        self.assertEqual(self.client.get("/resin-lots/").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)


class FutureAndEdgeTests(TestCase):
    """抽检日为未来时不计为当前有效检（按 sampledOn__lte 今天）。"""

    @classmethod
    def setUpTestData(cls):
        cls.worker = User.objects.create_user("worker", password="x")
        cls.lot = ResinLot.objects.create(
            lotCode="脂-FUT-1", originPlace="未来沟",
            arrivalKg=Decimal("100"), receivedAt=timezone.now(),
        )
        cls.today = timezone.localdate()

    def test_future_assay_not_counted(self):
        ImpurityAssay.objects.create(
            lot=self.lot, sampledOn=self.today + timedelta(days=1),
            impurityPct=Decimal("1.0"), passed=True,
            chemistName="化验", createdBy=self.worker,
        )
        self.assertIsNone(latest_active_assay(self.lot))
        with self.assertRaises(ValidationError):
            assert_lot_openable(self.lot)
