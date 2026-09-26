from django import forms
from django.utils import timezone

from .models import (
    CookRun,
    FireHearth,
    ImpurityAssay,
    ResinLot,
    SoftPointProbe,
)
from .services.assay_rules import (
    assay_status_map,
    assert_lot_openable,
    latest_active_assay,
)
from .services.floor_rules import assert_can_enter_drawing


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
        self.fields["resinLot"].queryset = ResinLot.objects.all()
        if not self.is_bound:
            self.initial["openedAt"] = timezone.localtime().strftime("%Y-%m-%dT%H:%M")
        # 开灶入口下拉即显示同源有效检判定，避免选了批才被拒。
        status_map = assay_status_map(list(self.fields["resinLot"].queryset))

        def _lot_label(obj):
            valid = status_map.get(obj.pk, (None, False))[1]
            return f"{obj.lotCode} · {'有效检' if valid else '无有效检'}"

        self.fields["resinLot"].label_from_instance = _lot_label

    def clean(self):
        cleaned = super().clean()
        if self.hearth is not None and self.hearth.open_run() is not None:
            raise forms.ValidationError("该灶已有进行中的值守，请先收灶再开新灶。")
        lot = cleaned.get("resinLot")
        opened_at = cleaned.get("openedAt")
        if lot is not None:
            # 挂批开灶门禁与界面「有效检」判定同源（assay_rules）。
            on_date = timezone.localdate(opened_at) if opened_at else None
            assert_lot_openable(lot, on_date=on_date)
        return cleaned


class ImpurityAssayForm(forms.ModelForm):
    class Meta:
        model = ImpurityAssay
        fields = ["lot", "sampledOn", "impurityPct", "passed", "chemistName"]
        widgets = {
            "lot": forms.Select(attrs={"class": "field"}),
            "sampledOn": forms.DateInput(
                attrs={"class": "field", "type": "date"},
                format="%Y-%m-%d",
            ),
            "impurityPct": forms.NumberInput(
                attrs={"class": "field", "step": "0.01", "min": "0", "max": "100"}
            ),
            "passed": forms.CheckboxInput(attrs={"class": "field"}),
            "chemistName": forms.TextInput(attrs={"class": "field"}),
        }
        help_texts = {
            "passed": "勾选表示抽检合格；只有通过检才可能成为有效检。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sampledOn"].input_formats = ["%Y-%m-%d"]
        self.fields["lot"].queryset = ResinLot.objects.all()
        if not self.is_bound:
            self.initial["sampledOn"] = timezone.localdate().strftime("%Y-%m-%d")

    def clean_impurityPct(self):
        pct = self.cleaned_data["impurityPct"]
        # 双保险（模型层已有 validator）：非负且不超过一百。
        if pct < 0 or pct > 100:
            raise forms.ValidationError("杂质百分数须在 0 到 100 之间。")
        return pct

    def clean(self):
        cleaned = super().clean()
        lot = cleaned.get("lot")
        sampled_on = cleaned.get("sampledOn")
        # 同批同一自然日只留一张；撞日拒绝并回显已有标识。
        if lot is not None and sampled_on is not None:
            existing = latest_active_assay(lot, on_date=sampled_on)
            # latest_active_assay 用 sampledOn__lte，需精确到同一自然日。
            if existing is not None and existing.sampledOn == sampled_on:
                raise forms.ValidationError(
                    f"该批 {sampled_on:%Y-%m-%d} 已有未作废抽检（编号 {existing}，"
                    f"化验人 {existing.chemistName}）；同日不得重复登记，"
                    "如需重检请先由主管作废原记录。"
                )
        return cleaned


def lot_assay_map(lots):
    """视图便捷封装：同源有效检判定。"""
    return assay_status_map(lots)
