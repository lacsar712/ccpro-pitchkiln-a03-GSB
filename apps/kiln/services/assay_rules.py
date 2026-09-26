"""来脂批杂质抽检规则。

开灶入口（OpenCookRunForm / open_run 视图）与界面「有效检」判定
（来脂批卡片、开灶表单）必须同源：一律走 latest_active_assay 与
is_assay_valid / assert_lot_openable，不得各写一份。
"""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

ASSAY_VALID_DAYS = 5


def latest_active_assay(lot, *, on_date=None):
    """该批最新一张未作废的抽检；无记录返回 None。"""
    if on_date is None:
        on_date = timezone.localdate()
    return (
        lot.assays.filter(voidedAt__isnull=True, sampledOn__lte=on_date)
        .order_by("-sampledOn", "-id")
        .first()
    )


def is_assay_valid(assay, *, on_date=None):
    """有效检：未作废、判定通过、抽检日不早于五个自然日前。"""
    if assay is None or assay.is_void or not assay.passed:
        return False
    if on_date is None:
        on_date = timezone.localdate()
    return assay.sampledOn >= on_date - timedelta(days=ASSAY_VALID_DAYS)


def assay_status(lot, *, on_date=None):
    """供界面同源展示：返回 (latest_assay|None, is_valid)。"""
    assay = latest_active_assay(lot, on_date=on_date)
    return assay, is_assay_valid(assay, on_date=on_date)


def assay_status_map(lots, *, on_date=None):
    """批量预取后给一批来脂批打状态：{lot_id: (assay, is_valid)}。"""
    from apps.kiln.models import ImpurityAssay

    if on_date is None:
        on_date = timezone.localdate()
    lot_ids = [lot.pk for lot in lots]
    latest = {}
    if lot_ids:
        active = ImpurityAssay.objects.filter(
            voidedAt__isnull=True, sampledOn__lte=on_date, lot_id__in=lot_ids
        ).order_by("-sampledOn", "-id")
        for assay in active:
            latest.setdefault(assay.lot_id, assay)
    return {
        lot.pk: (assay, is_assay_valid(assay, on_date=on_date))
        for lot in lots
        for assay in [latest.get(lot.pk)]
    }


def assert_lot_openable(lot, *, on_date=None):
    """挂批开灶前置：该批最新一张通过检且在五日有效窗内，否则中文拒绝。"""
    if on_date is None:
        on_date = timezone.localdate()
    assay = latest_active_assay(lot, on_date=on_date)

    if assay is None:
        raise ValidationError(
            {
                "resinLot": (
                    f"来脂批 {lot.lotCode} 尚无杂质抽检记录，"
                    "须先有一张有效检（五日内通过）方可开灶。"
                )
            }
        )
    if not assay.passed:
        raise ValidationError(
            {
                "resinLot": (
                    f"来脂批 {lot.lotCode} 最新抽检（{assay.sampledOn:%Y-%m-%d}，"
                    f"杂质 {assay.impurityPct}%，编号 {assay}）未通过，开灶被拒。"
                )
            }
        )
    if not is_assay_valid(assay, on_date=on_date):
        raise ValidationError(
            {
                "resinLot": (
                    f"来脂批 {lot.lotCode} 的通过检（抽检日 {assay.sampledOn:%Y-%m-%d}，"
                    f"编号 {assay}）已超出 {ASSAY_VALID_DAYS} 个自然日有效窗，"
                    "请重新抽检后再开灶。"
                )
            }
        )
    return assay
