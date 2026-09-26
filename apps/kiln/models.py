from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


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


class ImpurityAssay(models.Model):
    """来脂批杂质抽检：同批同一自然日只允许一张有效（未作废）记录。"""

    lot = models.ForeignKey(
        ResinLot,
        on_delete=models.PROTECT,
        related_name="assays",
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
    chemistName = models.CharField("化验人", max_length=80)
    createdBy = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="assays_created",
        verbose_name="登记人",
    )
    voidedAt = models.DateTimeField("作废时间", null=True, blank=True)
    voidedBy = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="assays_voided",
        verbose_name="作废人",
    )

    class Meta:
        ordering = ["-sampledOn", "-id"]
        verbose_name = "杂质抽检"
        verbose_name_plural = "杂质抽检"
        constraints = [
            models.UniqueConstraint(
                fields=["lot", "sampledOn"],
                condition=models.Q(voidedAt__isnull=True),
                name="uniq_active_assay_per_lot_day",
            ),
        ]

    def __str__(self):
        return f"杂检-{self.lot.lotCode}-{self.sampledOn:%Y%m%d}"

    @property
    def is_void(self):
        return self.voidedAt is not None

    def void(self, user):
        """主管作废；已作废的记录不得再当有效。"""
        if self.voidedAt is not None:
            return self
        self.voidedAt = timezone.now()
        self.voidedBy = user
        self.save(update_fields=["voidedAt", "voidedBy"])
        return self
