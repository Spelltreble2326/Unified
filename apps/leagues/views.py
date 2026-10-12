import io
import zipfile

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.decorators.http import require_http_methods, require_POST
from django.views.generic import CreateView, ListView

from apps.leagues.forms import LeagueForm
from apps.leagues.models import League, LeagueSnapshot
from apps.leagues.tasks import SyncEnqueueError, enqueue_league_sync


class LeagueListView(LoginRequiredMixin, ListView):
    model = League
    template_name = "leagues/league_list.html"
    context_object_name = "leagues"

    def get_queryset(self):
        return League.objects.filter(user=self.request.user).select_related("snapshot")


class LeagueCreateView(LoginRequiredMixin, CreateView):
    model = League
    form_class = LeagueForm
    template_name = "leagues/league_form.html"
    success_url = reverse_lazy("league-list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context["form"]
        platform = _selected_platform(self.request, form)
        context["platform"] = platform
        context["is_private"] = _is_private(self.request, form)
        context["fields_template"] = _fields_template(platform)
        return context

    def form_valid(self, form):
        form.instance.user = self.request.user
        response = super().form_valid(form)
        try:
            enqueue_league_sync(self.object)
        except SyncEnqueueError:
            pass
        return response


@login_required
def league_fields_partial(request):
    """HTMX fragment: the inputs that depend on the selected platform."""
    platform = request.GET.get("platform") or League.PLATFORM_ESPN
    if platform not in {League.PLATFORM_ESPN, League.PLATFORM_SLEEPER}:
        platform = League.PLATFORM_ESPN

    initial = {
        "platform": platform,
        "league_id": request.GET.get("league_id", ""),
        "name": request.GET.get("name", ""),
        "league_url": request.GET.get("league_url", ""),
        "espn_s2": request.GET.get("espn_s2", ""),
        "swid": request.GET.get("swid", ""),
    }
    form = LeagueForm(initial=initial, user=request.user)
    is_private = request.GET.get("is_private") == "on"
    return render(
        request,
        _fields_template(platform),
        {
            "form": form,
            "platform": platform,
            "is_private": is_private,
        },
    )


@login_required
@require_POST
def validate_league(request):
    """HTMX fragment: confirm the league id (and cookies) against the remote API."""
    form = LeagueForm(request.POST, user=request.user)
    if form.is_valid():
        return render(
            request,
            "leagues/_validate_result.html",
            {"ok": True, "name": form.cleaned_data["name"]},
        )
    return render(
        request,
        "leagues/_validate_result.html",
        {"ok": False, "errors": _form_error_messages(form)},
    )


@login_required
def league_roster(request, pk):
    league = get_object_or_404(League, pk=pk, user=request.user)
    snap = _snapshot(league)
    return render(
        request,
        "leagues/league_roster.html",
        {
            "league": league,
            "teams": snap.roster_data if snap else [],
            "synced_at": snap.synced_at if snap else None,
            "never_synced": snap is None,
        },
    )


@login_required
def league_activity(request, pk):
    league = get_object_or_404(League, pk=pk, user=request.user)
    snap = _snapshot(league)
    return render(
        request,
        "leagues/league_activity.html",
        {
            "league": league,
            "activities": snap.activity_data if snap else [],
            "error": snap.activity_error if snap else None,
            "synced_at": snap.synced_at if snap else None,
            "never_synced": snap is None,
        },
    )


@login_required
def league_matchups(request, pk):
    league = get_object_or_404(League, pk=pk, user=request.user)
    snap = _snapshot(league)
    return render(
        request,
        "leagues/league_matchups.html",
        {
            "league": league,
            "weeks": snap.matchup_data if snap else [],
            "synced_at": snap.synced_at if snap else None,
            "never_synced": snap is None,
        },
    )


@login_required
def league_standings(request, pk):
    league = get_object_or_404(League, pk=pk, user=request.user)
    snap = _snapshot(league)
    return render(
        request,
        "leagues/league_standings.html",
        {
            "league": league,
            "standings": snap.standings_data if snap else [],
            "synced_at": snap.synced_at if snap else None,
            "never_synced": snap is None,
        },
    )


@login_required
def league_matchup_detail(request, pk, week):
    league = get_object_or_404(League, pk=pk, user=request.user)
    snap = _snapshot(league)
    week_data = next(
        (row for row in (snap.matchup_data if snap else []) if row.get("week") == week),
        None,
    )
    if week_data is None:
        raise Http404
    return render(
        request,
        "leagues/league_matchup_detail.html",
        {
            "league": league,
            "week_data": week_data,
            "synced_at": snap.synced_at,
        },
    )


@login_required
@require_POST
def league_sync(request, pk):
    league = get_object_or_404(League, pk=pk, user=request.user)
    try:
        enqueue_league_sync(league)
    except SyncEnqueueError:
        pass
    league.refresh_from_db()
    if request.htmx:
        return render(request, "leagues/_sync_button.html", _sync_button_context(request, league))
    return redirect(request.META.get("HTTP_REFERER") or "league-list")


@login_required
def league_sync_status(request, pk):
    """HTMX poll target. Refreshes the page once the worker finishes."""
    league = get_object_or_404(League, pk=pk, user=request.user)
    response = render(request, "leagues/_sync_button.html", _sync_button_context(request, league))
    if request.htmx and not league.is_syncing:
        response["HX-Refresh"] = "true"
    return response


def _sync_button_context(request, league):
    return {
        "league": league,
        "show_meta": request.GET.get("meta") == "1",
    }


def _snapshot(league):
    try:
        return league.snapshot
    except LeagueSnapshot.DoesNotExist:
        return None


@login_required
def download_extension(request):
    """Zip the Chrome extension so a user can load it unpacked."""
    buf = io.BytesIO()
    ext_dir = settings.BASE_DIR / "extension"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in ext_dir.rglob("*"):
            if not path.is_file() or path.name.endswith(":Zone.Identifier"):
                continue
            zf.write(path, path.relative_to(ext_dir))
    buf.seek(0)
    response = HttpResponse(buf, content_type="application/zip")
    response["Content-Disposition"] = 'attachment; filename="espn-cookie-helper.zip"'
    return response


@login_required
@require_http_methods(["DELETE", "POST"])
def league_delete(request, pk):
    league = get_object_or_404(League, pk=pk, user=request.user)
    league.delete()
    if request.htmx:
        return HttpResponse("")
    return redirect("league-list")


def _selected_platform(request, form):
    if request.method == "POST":
        platform = request.POST.get("platform")
    else:
        platform = form["platform"].value()
    if platform not in {League.PLATFORM_ESPN, League.PLATFORM_SLEEPER}:
        return League.PLATFORM_ESPN
    return platform


def _is_private(request, form):
    if request.method == "POST":
        return request.POST.get("is_private") == "on"
    return bool(form["espn_s2"].value() and form["swid"].value())


def _fields_template(platform):
    if platform == League.PLATFORM_SLEEPER:
        return "leagues/_league_fields_sleeper.html"
    return "leagues/_league_fields_espn.html"


def _form_error_messages(form):
    messages = []
    for error in form.non_field_errors():
        messages.append(str(error))
    for field, errors in form.errors.items():
        if field == "__all__":
            continue
        label = form.fields[field].label or field
        for error in errors:
            messages.append(f"{label}: {error}")
    return messages
