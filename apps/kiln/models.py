from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


ASSAY_VALID_DAYS = 5


class ResinLot(models.Model):
    lotCode = models.CharField("来脂批号", max_length=64, unique=True)
    originPlace = models.CharField("来源地", max_length=120)
    arrivalKg = models.DecimalField("到货量(kg)", max_digits=10, decimal_places=2)
    receivedAt = models.DateTimeField("到货时间")

    class Meta:
        ordering = ["-receivedAt", "-id"]
        verbose_name = "来脂批"
        verbose_name_plural = "来脂批"

    def __str__(self):
        return f"{self.lotCode} · {self.originPlace}"


class FireHearth(models.Model):
    PHASE_COLD = "cold"
    PHASE_CHARGING = "charging"
    PHASE_RAMPING = "ramping"
    PHASE_HOLDING = "holding"
    PHASE_DRAWING = "drawing"
    PHASE_CHOICES = [
        (PHASE_COLD, "冷灶"),
        (PHASE_CHARGING, "装料"),
        (PHASE_RAMPING, "升温"),
        (PHASE_HOLDING, "保温"),
        (PHASE_DRAWING, "出胶"),
    ]

    lane = models.PositiveIntegerField("过道号")
    tag = models.CharField("灶牌", max_length=40, unique=True)
    resinGrade = models.CharField("松香品级标签", max_length=80)
    phase = models.CharField(
        "相位",
        max_length=20,
        choices=PHASE_CHOICES,
        default=PHASE_COLD,
    )

    class Meta:
        ordering = ["lane", "tag"]
        verbose_name = "灶台"
        verbose_name_plural = "灶台"

    def __str__(self):
        return f"L{self.lane}-{self.tag}"

    def open_run(self):
        return (
            self.runs.filter(closedAt__isnull=True)
            .select_related("resinLot")
            .order_by("-openedAt", "-id")
            .first()
        )


class CookRun(models.Model):
    hearth = models.ForeignKey(
        FireHearth,
        on_delete=models.CASCADE,
        related_name="runs",
        verbose_name="灶台",
    )
    resinLot = models.ForeignKey(
        ResinLot,
        on_delete=models.PROTECT,
        related_name="runs",
        verbose_name="来脂批",
    )
    openedAt = models.DateTimeField("开灶时间")
    closedAt = models.DateTimeField("收灶时间", null=True, blank=True)
    targetSoftPointC = models.DecimalField(
        "目标软化点(℃)", max_digits=6, decimal_places=2
    )

    class Meta:
        ordering = ["-openedAt", "-id"]
        verbose_name = "熬制值守"
        verbose_name_plural = "熬制值守"

    def __str__(self):
        return f"{self.hearth.tag} @ {self.openedAt:%Y-%m-%d %H:%M}"

    @property
    def is_open(self):
        return self.closedAt is None


class SoftPointProbe(models.Model):
    run = models.ForeignKey(
        CookRun,
        on_delete=models.CASCADE,
        related_name="probes",
        verbose_name="值守",
    )
    sampledAt = models.DateTimeField("取样时间")
    softPointC = models.DecimalField("软化点(℃)", max_digits=6, decimal_places=2)
    samplerName = models.CharField("取样人", max_length=80)

    class Meta:
        ordering = ["-sampledAt", "-id"]
        verbose_name = "软化点探针"
        verbose_name_plural = "软化点探针"

    def __str__(self):
        return f"{self.softPointC}℃ by {self.samplerName}"


class ImpurityTest(models.Model):
    """来脂批杂质抽检：开灶前该批须有一张仍有效的合格检。"""

    lot = models.ForeignKey(
        ResinLot,
        on_delete=models.PROTECT,
        related_name="impurity_tests",
        verbose_name="来脂批",
    )
    sampledOn = models.DateField("抽检日")
    impurityPct = models.DecimalField(
        "杂质百分数(%)",
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    passed = models.BooleanField("是否通过", default=False)
    chemist = models.CharField("化验人", max_length=80)
    recordedAt = models.DateTimeField("登记时间", auto_now_add=True)
    recordedBy = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recorded_impurity_tests",
        verbose_name="登记人",
        null=True,
        blank=True,
    )
    voidedAt = models.DateTimeField("作废时间", null=True, blank=True)
    voidedBy = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="voided_impurity_tests",
        verbose_name="作废人",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-sampledOn", "-id"]
        verbose_name = "杂质抽检"
        verbose_name_plural = "杂质抽检"
        constraints = [
            # 同批同一自然日只留一张有效检；作废检不再占位。
            models.UniqueConstraint(
                fields=["lot", "sampledOn"],
                condition=models.Q(voidedAt__isnull=True),
                name="uniq_active_impurity_test_per_lot_day",
            )
        ]

    def __str__(self):
        mark = "合格" if self.passed else "不合格"
        return f"脂检#{self.pk} {self.lot.lotCode} {self.sampledOn} {mark}"

    @property
    def is_void(self):
        return self.voidedAt is not None

    def is_stale(self, on_date=None):
        """抽检日早于开灶日的五个自然日前即过期（相隔恰为 5 天仍有效）。"""
        if self.sampledOn is None:
            return True
        if on_date is None:
            on_date = timezone.localdate()
        return self.sampledOn < on_date - timezone.timedelta(days=ASSAY_VALID_DAYS)

    def is_valid_open(self, on_date=None):
        """有效检判定（开灶入口与卡片标识同源）：未作废 + 通过 + 在五自然日窗内。"""
        return (not self.is_void) and self.passed and (not self.is_stale(on_date))
