"""杂质抽检（脂检）业务规则：登记、作废与开灶有效检判定。"""
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from ..models import ASSAY_VALID_DAYS, ImpurityTest


class AssayConflict(ValidationError):
    """同批同自然日已有一张有效检。"""

    def __init__(self, existing):
        self.existing = existing
        super().__init__(
            f"该来脂批 {existing.sampledOn} 已有有效脂检（编号 #{existing.pk}），"
            "同一自然日只留一张；如需重录请先由主管作废旧检。"
        )


def active_tests_for_lot(lot, on_date=None):
    """该批未作废检，最新在前。"""
    return lot.impurity_tests.filter(voidedAt__isnull=True).order_by(
        "-sampledOn", "-id"
    )


def latest_active_test(lot):
    return active_tests_for_lot(lot).first()


def valid_open_assay(lot, on_date=None):
    """
    返回该批当前可用于开灶的有效检，没有则 None。

    有效 = 最新一张未作废检「通过」且抽检日不早于五个自然日前。
    开灶入口与来脂批卡片标识共用此函数（同源）。
    """
    latest = latest_active_test(lot)
    if latest is not None and latest.is_valid_open(on_date):
        return latest
    return None


def assay_status(lot, on_date=None):
    """卡片标识用：(状态码, 对应脂检)。状态 ok|stale|fail|none。"""
    latest = latest_active_test(lot)
    if latest is None:
        return "none", None
    if latest.is_void:
        return "none", latest
    if not latest.passed:
        return "fail", latest
    if latest.is_stale(on_date):
        return "stale", latest
    return "ok", latest


def assay_status_map(lots, on_date=None):
    """批量取最新检，供卡片/下拉一次渲染：{lot_id: (状态码, 脂检或None)}。"""
    latest_by_lot = {}
    for test in ImpurityTest.objects.filter(
        lot_id__in=[lot.pk for lot in lots], voidedAt__isnull=True
    ).order_by("-sampledOn", "-id"):
        latest_by_lot.setdefault(test.lot_id, test)
    result = {}
    for lot in lots:
        latest = latest_by_lot.get(lot.pk)
        if latest is None:
            result[lot.pk] = ("none", None)
        elif not latest.passed:
            result[lot.pk] = ("fail", latest)
        elif latest.is_stale(on_date):
            result[lot.pk] = ("stale", latest)
        else:
            result[lot.pk] = ("ok", latest)
    return result


def assert_can_open_with_lot(lot, on_date=None):
    """开灶闸门：不满足即抛中文 ValidationError。"""
    if on_date is None:
        on_date = timezone.localdate()
    latest = latest_active_test(lot)

    if latest is None:
        raise ValidationError(
            f"来脂批 {lot.lotCode} 尚无有效杂质抽检，不能开灶；"
            "请先由值守工登记脂检。"
        )
    if not latest.passed:
        raise ValidationError(
            f"来脂批 {lot.lotCode} 最新脂检（#{latest.pk}，{latest.sampledOn}）"
            "判为不合格，不能开灶。"
        )
    if latest.is_stale(on_date):
        earliest = on_date - timezone.timedelta(days=ASSAY_VALID_DAYS)
        raise ValidationError(
            f"来脂批 {lot.lotCode} 最新合格脂检（#{latest.pk}，"
            f"{latest.sampledOn}）已超出 {ASSAY_VALID_DAYS} 日有效窗"
            f"（抽检日不早于 {earliest:%Y-%m-%d}），请重新抽检。"
        )


@transaction.atomic
def record_assay(*, lot, sampled_on, impurity_pct, passed, chemist, user):
    """登记脂检；同批同自然日撞单则拒绝并回显已有检标识。"""
    existing = (
        ImpurityTest.objects.select_for_update()
        .filter(lot=lot, sampledOn=sampled_on, voidedAt__isnull=True)
        .first()
    )
    if existing is not None:
        raise AssayConflict(existing)

    test = ImpurityTest(
        lot=lot,
        sampledOn=sampled_on,
        impurityPct=impurity_pct,
        passed=passed,
        chemist=chemist,
        recordedBy=user if user and user.is_authenticated else None,
    )
    try:
        test.save()
    except IntegrityError:
        existing = (
            ImpurityTest.objects.filter(
                lot=lot, sampledOn=sampled_on, voidedAt__isnull=True
            )
            .order_by("id")
            .first()
        )
        raise AssayConflict(existing) if existing else ValidationError("脂检登记失败。")
    return test


@transaction.atomic
def void_assay(test, *, user):
    """主管作废：作废检不得再当有效。"""
    test = ImpurityTest.objects.select_for_update().get(pk=test.pk)
    if test.voidedAt is not None:
        raise ValidationError(f"脂检 #{test.pk} 已作废，不能重复作废。")
    test.voidedAt = timezone.now()
    test.voidedBy = user if user and user.is_authenticated else None
    test.save(update_fields=["voidedAt", "voidedBy"])
    return test
