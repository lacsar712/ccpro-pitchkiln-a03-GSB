from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import Prefetch
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .forms import (
    ImpurityAssayForm,
    OpenCookRunForm,
    PhaseChangeForm,
    ResinLotForm,
    SoftPointProbeForm,
    lot_assay_map,
)
from .models import CookRun, FireHearth, ImpurityAssay, ResinLot
from .services.assay_rules import assert_lot_openable, latest_active_assay
from .services.floor_rules import change_hearth_phase


def _wants_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _hearths_for_board():
    return FireHearth.objects.prefetch_related(
        Prefetch(
            "runs",
            queryset=CookRun.objects.filter(closedAt__isnull=True)
            .select_related("resinLot")
            .prefetch_related("probes"),
            to_attr="open_runs_cache",
        )
    ).order_by("lane", "tag")


def _board_context():
    hearths = list(_hearths_for_board())
    lanes = {}
    for h in hearths:
        lanes.setdefault(h.lane, []).append(h)
    phase_legend = [
        (key, label, sum(1 for h in hearths if h.phase == key))
        for key, label in FireHearth.PHASE_CHOICES
    ]
    return {
        "hearths": hearths,
        "lanes": sorted(lanes.items()),
        "phase_legend": phase_legend,
    }


def _drawer_context(hearth):
    open_run = hearth.open_run()
    probes = []
    if open_run:
        probes = list(open_run.probes.order_by("-sampledAt", "-id"))
    return {
        "hearth": hearth,
        "open_run": open_run,
        "probes": probes,
        "phase_form": PhaseChangeForm(hearth=hearth),
        "probe_form": SoftPointProbeForm() if open_run else None,
        "open_run_form": OpenCookRunForm(hearth=hearth) if open_run is None else None,
    }


@login_required
def home(request):
    ctx = _board_context()
    drawer_pk = request.GET.get("hearth")
    if drawer_pk:
        try:
            hearth = FireHearth.objects.get(pk=drawer_pk)
            ctx.update(_drawer_context(hearth))
            ctx["drawer_open"] = True
        except (FireHearth.DoesNotExist, ValueError):
            ctx["drawer_open"] = False
    else:
        ctx["drawer_open"] = False
    return render(request, "floor/board.html", ctx)


@login_required
def floor_grid_partial(request):
    html = render_to_string("floor/_grid.html", _board_context(), request=request)
    return HttpResponse(html)


@login_required
def hearth_drawer(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    ctx = _drawer_context(hearth)
    if _wants_htmx(request):
        return render(request, "floor/_drawer.html", ctx)
    return redirect(f"/?hearth={pk}")


@login_required
@require_POST
def change_phase(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    form = PhaseChangeForm(request.POST, hearth=hearth)
    if form.is_valid():
        try:
            change_hearth_phase(hearth, form.cleaned_data["phase"])
            messages.success(request, f"灶牌 {hearth.tag} 相位已更新")
        except ValidationError as exc:
            msg = (
                exc.message_dict.get("phase") if hasattr(exc, "message_dict") else None
            )
            messages.error(request, msg[0] if msg else str(exc))
    else:
        err = form.errors.get("phase")
        messages.error(request, err[0] if err else "相位切换失败")

    if _wants_htmx(request):
        hearth.refresh_from_db()
        resp = render(request, "floor/_drawer.html", _drawer_context(hearth))
        resp["HX-Trigger"] = "floor-refresh"
        return resp
    return redirect(f"/?hearth={pk}")


@login_required
@require_POST
def add_probe(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    open_run = hearth.open_run()
    if open_run is None:
        messages.error(request, "没有进行中的值守，无法登记探针")
        return redirect(f"/?hearth={pk}")

    form = SoftPointProbeForm(request.POST)
    if form.is_valid():
        probe = form.save(commit=False)
        probe.run = open_run
        probe.save()
        messages.success(request, f"已登记探针 {probe.softPointC}℃")
    else:
        messages.error(request, "探针登记失败，请检查输入")

    if _wants_htmx(request):
        resp = render(request, "floor/_drawer.html", _drawer_context(hearth))
        resp["HX-Trigger"] = "floor-refresh"
        return resp
    return redirect(f"/?hearth={pk}")


@login_required
@require_POST
def open_run(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    form = OpenCookRunForm(request.POST, hearth=hearth)
    if form.is_valid():
        run = form.save(commit=False)
        run.hearth = hearth
        # 绕过表单直接调用视图也必失败：开灶与有效检判定同源。
        try:
            assert_lot_openable(
                run.resinLot, on_date=timezone.localdate(run.openedAt)
            )
        except ValidationError as exc:
            msg = (
                exc.message_dict.get("resinLot")
                if hasattr(exc, "message_dict")
                else None
            )
            messages.error(request, msg[0] if msg else str(exc))
        else:
            run.save()
            if hearth.phase == FireHearth.PHASE_COLD:
                hearth.phase = FireHearth.PHASE_CHARGING
                hearth.save(update_fields=["phase"])
            messages.success(request, "新值守已开灶")
    else:
        for errs in form.errors.values():
            for e in errs:
                messages.error(request, e)
            break

    if _wants_htmx(request):
        hearth.refresh_from_db()
        resp = render(request, "floor/_drawer.html", _drawer_context(hearth))
        resp["HX-Trigger"] = "floor-refresh"
        return resp
    return redirect(f"/?hearth={pk}")


@login_required
@require_POST
def close_run(request, pk):
    hearth = get_object_or_404(FireHearth, pk=pk)
    open_run = hearth.open_run()
    if open_run is None:
        messages.error(request, "没有进行中的值守可收灶")
    else:
        open_run.closedAt = timezone.now()
        open_run.save(update_fields=["closedAt"])
        hearth.phase = FireHearth.PHASE_COLD
        hearth.save(update_fields=["phase"])
        messages.success(request, "值守已收灶，灶台回冷灶")

    if _wants_htmx(request):
        hearth.refresh_from_db()
        resp = render(request, "floor/_drawer.html", _drawer_context(hearth))
        resp["HX-Trigger"] = "floor-refresh"
        return resp
    return redirect(f"/?hearth={pk}")


@login_required
@require_http_methods(["GET", "POST"])
def resin_lot_feed(request):
    if request.method == "POST":
        form = ResinLotForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "来脂批已登记")
            return redirect("resin_lot_feed")
    else:
        form = ResinLotForm(
            initial={
                "receivedAt": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            }
        )

    lots = list(ResinLot.objects.all()[:40])
    status_map = lot_assay_map(lots)
    lot_rows = [
        {"lot": lot, "assay": status_map[lot.pk][0], "valid": status_map[lot.pk][1]}
        for lot in lots
    ]
    return render(
        request,
        "resin/feed.html",
        {"lot_rows": lot_rows, "form": form},
    )


@login_required
@require_http_methods(["GET", "POST"])
def assay_feed(request):
    """脂检台账：值守工可登记杂质抽检。"""
    if request.method == "POST":
        form = ImpurityAssayForm(request.POST)
        if form.is_valid():
            assay = form.save(commit=False)
            assay.createdBy = request.user
            # 竞态兜底：并发同日双提交时部分唯一约束拦住第二张。
            try:
                assay.save()
            except IntegrityError:
                existing = latest_active_assay(
                    assay.lot, on_date=assay.sampledOn
                )
                tag = f"（编号 {existing}）" if existing else ""
                messages.error(
                    request,
                    f"该批 {assay.sampledOn:%Y-%m-%d} 已有未作废抽检{tag}，"
                    "同日不得重复登记。",
                )
            else:
                messages.success(request, f"杂质抽检已登记（编号 {assay}）")
            return redirect("assay_feed")
    else:
        form = ImpurityAssayForm()

    assays = list(
        ImpurityAssay.objects.select_related("lot", "createdBy", "voidedBy").all()[:60]
    )
    # 同源判定：只有「该批当前有效那张检」才打有效检标。
    lots = ResinLot.objects.filter(pk__in={a.lot_id for a in assays})
    status_map = lot_assay_map(list(lots))
    rows = []
    for a in assays:
        latest_a, valid_flag = status_map.get(a.lot_id, (None, False))
        is_latest_active = latest_a == a
        rows.append(
            {
                "assay": a,
                "valid": is_latest_active and valid_flag,
                "is_latest_active": is_latest_active,
            }
        )
    return render(
        request,
        "resin/assay_feed.html",
        {"rows": rows, "form": form},
    )


@login_required
@require_POST
def void_assay(request, pk):
    """主管可作废；作废检不得再当有效。值守工无权。"""
    assay = get_object_or_404(ImpurityAssay, pk=pk)
    if not request.user.is_staff:
        messages.error(request, "只有主管可以作废抽检记录。")
        return redirect("assay_feed")
    if assay.is_void:
        messages.error(request, f"编号 {assay} 已作废，不能重复作废。")
        return redirect("assay_feed")
    assay.void(request.user)
    messages.success(request, f"抽检 {assay} 已作废，不再计为有效检。")
    return redirect("assay_feed")
