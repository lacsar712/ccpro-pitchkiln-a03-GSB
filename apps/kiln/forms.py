from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import CookRun, FireHearth, ImpurityTest, ResinLot, SoftPointProbe
from .services.assays import (
    assay_status_map,
    assert_can_open_with_lot,
)
from .services.floor_rules import assert_can_enter_drawing

_LOT_STATUS_LABEL = {
    "ok": "有效脂检",
    "stale": "脂检超五日窗",
    "fail": "最新脂检不合格",
    "none": "无有效脂检",
}


def _lot_choices(with_status=False):
    lots = list(ResinLot.objects.all())
    statuses = assay_status_map(lots) if with_status else {}
    choices = []
    for lot in lots:
        label = f"{lot.lotCode} · {lot.originPlace}"
        if with_status:
            code = statuses.get(lot.pk, ("none", None))[0]
            label = f"{label}（{_LOT_STATUS_LABEL[code]}）"
        choices.append((lot.pk, label))
    return choices


class ResinLotForm(forms.ModelForm):
    class Meta:
        model = ResinLot
        fields = ["lotCode", "originPlace", "arrivalKg", "receivedAt"]
        widgets = {
            "lotCode": forms.TextInput(attrs={"class": "field"}),
            "originPlace": forms.TextInput(attrs={"class": "field"}),
            "arrivalKg": forms.NumberInput(attrs={"class": "field", "step": "0.01"}),
            "receivedAt": forms.DateTimeInput(
                attrs={"class": "field", "type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["receivedAt"].input_formats = [
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ]
        if self.instance and self.instance.pk and self.instance.receivedAt:
            local = timezone.localtime(self.instance.receivedAt)
            self.initial["receivedAt"] = local.strftime("%Y-%m-%dT%H:%M")


class PhaseChangeForm(forms.Form):
    phase = forms.ChoiceField(
        label="相位",
        choices=FireHearth.PHASE_CHOICES,
        widget=forms.Select(attrs={"class": "field"}),
    )

    def __init__(self, *args, hearth=None, **kwargs):
        self.hearth = hearth
        super().__init__(*args, **kwargs)
        if hearth is not None and not self.is_bound:
            self.fields["phase"].initial = hearth.phase

    def clean_phase(self):
        phase = self.cleaned_data["phase"]
        if self.hearth is not None and phase == FireHearth.PHASE_DRAWING:
            assert_can_enter_drawing(self.hearth)
        return phase


class SoftPointProbeForm(forms.ModelForm):
    class Meta:
        model = SoftPointProbe
        fields = ["sampledAt", "softPointC", "samplerName"]
        widgets = {
            "sampledAt": forms.DateTimeInput(
                attrs={"class": "field", "type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
            "softPointC": forms.NumberInput(attrs={"class": "field", "step": "0.01"}),
            "samplerName": forms.TextInput(attrs={"class": "field"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sampledAt"].input_formats = [
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ]
        if not self.is_bound and not (self.instance and self.instance.pk):
            self.initial["sampledAt"] = timezone.localtime().strftime("%Y-%m-%dT%H:%M")


class ImpurityTestForm(forms.ModelForm):
    class Meta:
        model = ImpurityTest
        fields = ["lot", "sampledOn", "impurityPct", "passed", "chemist"]
        widgets = {
            "lot": forms.Select(attrs={"class": "field"}),
            "sampledOn": forms.DateInput(
                attrs={"class": "field", "type": "date"},
                format="%Y-%m-%d",
            ),
            "impurityPct": forms.NumberInput(
                attrs={"class": "field", "step": "0.01", "min": "0", "max": "100"}
            ),
            "passed": forms.CheckboxInput(attrs={"class": "field-check"}),
            "chemist": forms.TextInput(attrs={"class": "field"}),
        }
        help_texts = {
            "impurityPct": "取值 0～100。合格脂检自抽检日起五自然日内有效。",
            "passed": "勾选表示该批杂质抽检通过。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lot"].choices = _lot_choices(with_status=True)
        self.fields["sampledOn"].input_formats = ["%Y-%m-%d"]
        if not self.is_bound and not (self.instance and self.instance.pk):
            self.initial["sampledOn"] = timezone.localdate().strftime("%Y-%m-%d")


class OpenCookRunForm(forms.ModelForm):
    class Meta:
        model = CookRun
        fields = ["resinLot", "openedAt", "targetSoftPointC"]
        widgets = {
            "resinLot": forms.Select(attrs={"class": "field"}),
            "openedAt": forms.DateTimeInput(
                attrs={"class": "field", "type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
            "targetSoftPointC": forms.NumberInput(
                attrs={"class": "field", "step": "0.01"}
            ),
        }

    def __init__(self, *args, hearth=None, **kwargs):
        self.hearth = hearth
        super().__init__(*args, **kwargs)
        self.fields["openedAt"].input_formats = [
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ]
        self.fields["resinLot"].choices = _lot_choices(with_status=True)
        if not self.is_bound:
            self.initial["openedAt"] = timezone.localtime().strftime("%Y-%m-%dT%H:%M")

    def clean(self):
        cleaned = super().clean()
        if self.hearth is not None and self.hearth.open_run() is not None:
            raise forms.ValidationError("该灶已有进行中的值守，请先收灶再开新灶。")
        lot = cleaned.get("resinLot")
        if lot is not None:
            try:
                assert_can_open_with_lot(lot)
            except ValidationError as exc:
                self.add_error("resinLot", exc.messages[0])
        return cleaned
