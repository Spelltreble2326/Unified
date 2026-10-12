"""Look up a remote fantasy league so we can confirm the id before saving."""

import json
import re
from datetime import date, datetime, timezone as dt_timezone
from pathlib import Path

import requests
from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from apps.leagues.models import League, LeagueSnapshot

SLEEPER_API = "https://api.sleeper.app"
_SLEEPER_PLAYERS = None
_NON_STARTING_SLOTS = {"BN", "BE", "IR", "TAXI"}

# Short labels for scoring keys from Sleeper (snake_case) and ESPN (camelCase).
_STAT_LABELS = {
    "pass_yd": "Pass Yds",
    "pass_td": "Pass TD",
    "pass_int": "Interception thrown",
    "pass_2pt": "Passing 2pt",
    "rush_yd": "Rush Yds",
    "rush_td": "Rush TD",
    "rush_2pt": "Rushing 2pt",
    "rec": "Reception",
    "rec_yd": "Rec Yds",
    "rec_td": "Rec TD",
    "rec_2pt": "Receiving 2pt",
    "fum_lost": "Fumble lost",
    "fum_rec": "Fumble recovery",
    "fum_rec_td": "Fumble rec TD",
    "bonus_rush_rec_yd_100": "100+ rush/rec yd bonus",
    "bonus_rush_yd_100": "100+ rush yd bonus",
    "bonus_rec_yd_100": "100+ rec yd bonus",
    "bonus_pass_yd_300": "300+ pass yd bonus",
    "xpm": "Extra point made",
    "xpmiss": "Extra point miss",
    "fgm_0_19": "FG 0-19",
    "fgm_20_29": "FG 20-29",
    "fgm_30_39": "FG 30-39",
    "fgm_40_49": "FG 40-49",
    "fgm_50_59": "FG 50-59",
    "fgm_60p": "FG 60+",
    "fgmiss": "FG miss",
    "sack": "Sack",
    "int": "Def interception",
    "ff": "Forced fumble",
    "safe": "Safety",
    "def_td": "Def TD",
    "def_st_td": "Def/ST TD",
    "st_td": "ST TD",
    "blk_kick": "Blocked kick",
    "def_st_ff": "Def/ST forced fumble",
    "def_st_fum_rec": "Def/ST fumble rec",
    "st_ff": "ST forced fumble",
    "st_fum_rec": "ST fumble rec",
    "pts_allow_0": "Pts allowed 0",
    "pts_allow_1_6": "Pts allowed 1-6",
    "pts_allow_7_13": "Pts allowed 7-13",
    "pts_allow_14_20": "Pts allowed 14-20",
    "pts_allow_21_27": "Pts allowed 21-27",
    "pts_allow_28_34": "Pts allowed 28-34",
    "pts_allow_35p": "Pts allowed 35+",
    "passingYards": "Pass Yds",
    "passingTouchdowns": "Pass TD",
    "passingInterceptions": "Interception thrown",
    "passing2PtConversions": "Passing 2pt",
    "rushingYards": "Rush Yds",
    "rushingTouchdowns": "Rush TD",
    "rushing2PtConversions": "Rushing 2pt",
    "receivingReceptions": "Reception",
    "receivingYards": "Rec Yds",
    "receivingTouchdowns": "Rec TD",
    "receiving2PtConversions": "Receiving 2pt",
    "lostFumbles": "Fumble lost",
    "madeExtraPoints": "Extra point made",
    "missedExtraPoints": "Extra point miss",
    "madeFieldGoalsFromUnder40": "FG 0-39",
    "madeFieldGoalsFrom40To49": "FG 40-49",
    "madeFieldGoalsFrom50Plus": "FG 50+",
    "missedFieldGoals": "FG miss",
}


class LeagueLookupError(Exception):
    """The remote league could not be loaded."""


def lookup_league(*, platform, league_id, espn_s2="", swid="", year=None):
    """Return the league's display name from ESPN or Sleeper.

    Raises LeagueLookupError with a message safe to show in the form.
    """
    league_id = (league_id or "").strip()
    if not league_id:
        raise LeagueLookupError("Enter a league ID.")

    if platform == League.PLATFORM_SLEEPER:
        return _lookup_sleeper(league_id)
    if platform == League.PLATFORM_ESPN:
        return _lookup_espn(league_id, espn_s2=espn_s2, swid=swid, year=year)
    raise LeagueLookupError("Choose ESPN or Sleeper.")


def _lookup_sleeper(league_id):
    url = f"{SLEEPER_API}/v1/league/{league_id}"
    try:
        response = requests.get(url, timeout=15)
    except requests.RequestException as exc:
        raise LeagueLookupError(f"Could not reach Sleeper: {exc}") from exc

    if response.status_code == 404:
        raise LeagueLookupError(f"Sleeper league {league_id} was not found.")
    if response.status_code != 200:
        raise LeagueLookupError(
            f"Sleeper returned HTTP {response.status_code} for that league."
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise LeagueLookupError("Sleeper returned a response that was not JSON.") from exc

    if not data:
        raise LeagueLookupError(f"Sleeper league {league_id} was not found.")

    sport = data.get("sport")
    if sport and sport != "nfl":
        raise LeagueLookupError(
            f"That Sleeper league is {sport}, not NFL."
        )

    return data.get("name") or f"Sleeper {league_id}"


def _lookup_espn(league_id, *, espn_s2, swid, year):
    league = _open_espn(league_id, espn_s2=espn_s2, swid=swid, year=year)
    league_settings = getattr(league, "settings", None)
    name = getattr(league_settings, "name", None) if league_settings else None
    return name or f"ESPN {league_id}"


def fetch_rosters(league):
    """Return each team's roster as ``{"team", "players"}`` dicts.

    Players are ``{"name", "position", "nfl_team", "acquisition"}``.
    Raises LeagueLookupError when the remote league cannot be loaded.
    """
    if league.platform == League.PLATFORM_SLEEPER:
        return _sleeper_rosters(league.league_id)
    if league.platform == League.PLATFORM_ESPN:
        return _espn_rosters(league)
    raise LeagueLookupError("Choose ESPN or Sleeper.")


def fetch_activities(league, since=None):
    """Return ``{"activities": [...], "error": None}`` or an error envelope.

    Each activity is ``{"date", "team", "action", "player", "bid"}``, newest
    first. ``since`` limits the pull to moves after that time. With ``since``
    omitted, the full season history is fetched. This does not raise; ESPN
    auth failures land in ``error``.
    """
    try:
        if league.platform == League.PLATFORM_SLEEPER:
            activities = _sleeper_activities(league.league_id, since=since)
        elif league.platform == League.PLATFORM_ESPN:
            activities, error = _espn_activities(league, since=since)
            return {"activities": activities, "error": error}
        else:
            raise LeagueLookupError("Choose ESPN or Sleeper.")
    except LeagueLookupError as exc:
        return {"activities": [], "error": str(exc)}
    return {"activities": activities, "error": None}


def sync_league(league):
    """Refresh the roster, activity, and matchup snapshot for a league.

    The first successful sync, and any sync after an activity error, stores
    the full activity history. Later syncs append only moves newer than
    ``synced_at`` and keep the history already saved.

    Matchup weeks already stored are kept. Only newly completed weeks are
    fetched and prepended.

    Returns the snapshot, or ``None`` when the league was removed while the
    remote data was being fetched.
    """
    snap = _existing_snapshot(league)
    full_history = snap is None or bool(snap.activity_error)
    since = None if full_history else snap.synced_at
    existing = [] if full_history else list(snap.activity_data or [])

    try:
        teams = fetch_rosters(league)
    except LeagueLookupError:
        teams = list(snap.roster_data) if snap else []

    try:
        standings = fetch_standings(league)
    except LeagueLookupError:
        standings = list(snap.standings_data) if snap else []

    result = fetch_activities(league, since=since)
    error = result.get("error") or ""
    new_rows = result["activities"]
    if full_history:
        if new_rows:
            activities = new_rows
        elif error and snap:
            activities = list(snap.activity_data or [])
        else:
            activities = []
    elif new_rows:
        activities = _merge_activities(new_rows, existing)
    else:
        activities = existing

    stored_matchups = list(snap.matchup_data or []) if snap else []
    stored_weeks = {
        row.get("week") for row in stored_matchups if row.get("week") is not None
    }
    try:
        fetched_matchups = fetch_matchups(
            league,
            completed_weeks=None if snap is None else stored_weeks,
        )
    except LeagueLookupError:
        fetched_matchups = []
    matchups = _merge_matchup_data(fetched_matchups, stored_matchups)

    # Fetching matchups can take long enough for the user to remove the
    # league. SQLite checks that foreign key at commit, which would 500.
    if not League.objects.filter(pk=league.pk).exists():
        return None
    try:
        snapshot, _created = LeagueSnapshot.objects.update_or_create(
            league=league,
            defaults={
                "roster_data": teams,
                "activity_data": activities,
                "activity_error": error,
                "matchup_data": matchups,
                "standings_data": standings,
            },
        )
    except IntegrityError:
        if not League.objects.filter(pk=league.pk).exists():
            return None
        raise
    return snapshot


def fetch_standings(league):
    """Return a ranked standings list for the league.

    Each entry is::

        {"rank": 1, "team": "Name", "wins": 5, "losses": 2, "ties": 0,
         "points_for": 842.50, "points_against": 710.25}

    Raises LeagueLookupError when the remote league cannot be loaded.
    """
    if league.platform == League.PLATFORM_SLEEPER:
        return _sleeper_standings(league.league_id)
    if league.platform == League.PLATFORM_ESPN:
        return _espn_standings(league)
    raise LeagueLookupError("Choose ESPN or Sleeper.")


def _espn_standings(league):
    remote = _open_espn(league.league_id, espn_s2=league.espn_s2, swid=league.swid)
    rows = []
    for team in remote.teams or []:
        rows.append(
            {
                "team": getattr(team, "team_name", None) or str(team),
                "wins": int(getattr(team, "wins", 0) or 0),
                "losses": int(getattr(team, "losses", 0) or 0),
                "ties": int(getattr(team, "ties", 0) or 0),
                "points_for": _round_points(getattr(team, "points_for", 0)),
                "points_against": _round_points(getattr(team, "points_against", 0)),
            }
        )
    rows.sort(key=lambda r: (-r["wins"], r["losses"], -r["points_for"]))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def _sleeper_standings(league_id):
    rosters, owners = _sleeper_roster_context(league_id)
    rows = []
    for roster in rosters:
        settings = roster.get("settings") or {}
        # Sleeper stores integer points and decimal parts separately.
        fpts = float(settings.get("fpts") or 0) + float(
            settings.get("fpts_decimal") or 0
        ) / 100
        fpts_against = float(settings.get("fpts_against") or 0) + float(
            settings.get("fpts_against_decimal") or 0
        ) / 100
        rows.append(
            {
                "team": owners.get(roster.get("roster_id"), "(unowned)"),
                "wins": int(settings.get("wins") or 0),
                "losses": int(settings.get("losses") or 0),
                "ties": int(settings.get("ties") or 0),
                "points_for": round(fpts, 2),
                "points_against": round(fpts_against, 2),
            }
        )
    rows.sort(key=lambda r: (-r["wins"], r["losses"], -r["points_for"]))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def fetch_matchups(league, completed_weeks=None):
    """Return matchup data for completed weeks that are not already stored.

    ``completed_weeks`` is the set of week numbers already on the snapshot.
    Those weeks are skipped so a later sync only pulls weeks that have
    finished since the last snapshot. ``None`` fetches every completed week.

    Weeks come back newest first. Each one is::

        {"week": 3, "matchups": [{matchup_id, home, away, winner, is_bye}]}
    """
    if league.platform == League.PLATFORM_SLEEPER:
        return _sleeper_matchups(league.league_id, completed_weeks)
    if league.platform == League.PLATFORM_ESPN:
        return _espn_matchups(league, completed_weeks)
    raise LeagueLookupError("Choose ESPN or Sleeper.")


def _merge_matchup_data(new_rows, existing_rows):
    """Combine week records. A newly fetched week replaces the stored copy."""
    by_week = {}
    for row in list(existing_rows or []) + list(new_rows or []):
        week = row.get("week")
        if week is None:
            continue
        by_week[week] = row
    merged = list(by_week.values())
    merged.sort(key=lambda row: row.get("week") or 0, reverse=True)
    return merged


def _espn_matchups(league, skip_weeks):
    remote = _open_espn(
        league.league_id, espn_s2=league.espn_s2, swid=league.swid
    )
    current = int(getattr(remote, "current_week", None) or 1)
    print(f"[DEBUG] ESPN matchups: league={league.league_id}, current_week={current}, season={remote.year}, skip_weeks={skip_weeks}")
    skip = set(skip_weeks or [])
    player_team_cache = {}
    weeks = []
    for week in range(1, current):
        if week in skip:
            print(f"[DEBUG]   week {week}: skipped (already stored)")
            continue
        try:
            boxes = remote.box_scores(week, player_team_cache=player_team_cache) or []
            print(f"[DEBUG]   week {week}: got {len(boxes)} box_scores")
        except (KeyError, ValueError, AttributeError) as exc:
            # ESPN data might be incomplete for that week (scoring not finalized, etc.)
            print(f"[DEBUG]   week {week}: incomplete data ({type(exc).__name__}: {exc}), treating as empty week")
            boxes = []
        except Exception as exc:
            print(f"[DEBUG]   week {week}: exception {exc}")
            if weeks:
                break
            raise LeagueLookupError(
                f"Could not load ESPN week {week} matchups: {exc}"
            ) from exc
        matchups = []
        for index, box in enumerate(boxes, start=1):
            home = _espn_side(
                getattr(box, "home_team", None),
                getattr(box, "home_score", 0),
                getattr(box, "home_lineup", None),
            )
            away = _espn_side(
                getattr(box, "away_team", None),
                getattr(box, "away_score", 0),
                getattr(box, "away_lineup", None),
            )
            matchups.append(_matchup_record(index, home, away))
        weeks.append({"week": week, "matchups": matchups})
    weeks.sort(key=lambda row: row["week"], reverse=True)
    return weeks


def _espn_side(team, score, lineup):
    if team is None:
        return None
    name = getattr(team, "team_name", None) or str(team)
    try:
        total = round(float(score or 0), 2)
    except (TypeError, ValueError):
        total = 0.0
    return {"team": name, "score": total, "starters": _espn_starters(lineup)}


def _espn_starters(lineup):
    rows = []
    for player in lineup or []:
        slot = getattr(player, "slot_position", "") or ""
        if slot in _NON_STARTING_SLOTS:
            continue
        breakdown = _espn_breakdown(player)
        row = {
            "name": getattr(player, "name", None) or "?",
            "position": getattr(player, "position", None) or "",
            "slot": slot or "?",
            "points": _round_points(getattr(player, "points", 0)),
        }
        if breakdown:
            row["breakdown"] = breakdown
        rows.append(row)
    return rows


def _espn_breakdown(player):
    """Pair ESPN raw stat values with the points each stat scored."""
    values = getattr(player, "breakdown", None) or {}
    points = getattr(player, "points_breakdown", None) or {}
    if not points:
        return []
    rows = []
    for stat, pts in points.items():
        try:
            pts = float(pts or 0)
        except (TypeError, ValueError):
            continue
        if not pts:
            continue
        try:
            value = float(values.get(stat, 0) or 0)
        except (TypeError, ValueError):
            value = 0.0
        weight = (pts / value) if value else 0.0
        rows.append(_breakdown_row(stat, value, weight, pts))
    rows.sort(key=lambda row: abs(row["points"]), reverse=True)
    return rows


def _sleeper_matchups(league_id, skip_weeks):
    info = _sleeper_get(f"/v1/league/{league_id}")
    if not info:
        raise LeagueLookupError(f"Sleeper league {league_id} was not found.")
    settings = info.get("settings") or {}
    leg = int(settings.get("leg") or 1)
    season = info.get("season") or date.today().year
    scoring = info.get("scoring_settings") or {}
    slots = [
        slot
        for slot in (info.get("roster_positions") or [])
        if slot not in _NON_STARTING_SLOTS
    ]
    _, owners = _sleeper_roster_context(league_id)
    players = _sleeper_players()
    stats_cache = {}
    skip = set(skip_weeks or [])
    weeks = []
    for week in range(1, leg):
        if week in skip:
            continue
        try:
            raw = _sleeper_get(f"/v1/league/{league_id}/matchups/{week}")
        except LeagueLookupError:
            if weeks:
                break
            raise
        if not isinstance(raw, list) or not raw:
            continue
        weeks.append(
            {
                "week": week,
                "matchups": _pair_sleeper_matchups(
                    raw,
                    owners,
                    players,
                    slots,
                    scoring,
                    season,
                    week,
                    stats_cache,
                ),
            }
        )
    weeks.sort(key=lambda row: row["week"], reverse=True)
    return weeks


def _pair_sleeper_matchups(
    entries, owners, players, slots, scoring, season, week, stats_cache
):
    grouped = {}
    for entry in entries:
        grouped.setdefault(entry.get("matchup_id"), []).append(entry)

    matchups = []
    for matchup_id in sorted(grouped, key=_matchup_id_sort):
        pair = sorted(grouped[matchup_id], key=lambda row: row.get("roster_id") or 0)
        sides = [
            _sleeper_side(
                entry, owners, players, slots, scoring, season, week, stats_cache
            )
            for entry in pair
        ]
        step = 0
        while step < len(sides):
            home = sides[step]
            away = sides[step + 1] if step + 1 < len(sides) else None
            record_id = matchup_id if step == 0 else f"{matchup_id}:{step}"
            matchups.append(_matchup_record(record_id, home, away))
            step += 2
    return matchups


def _sleeper_side(entry, owners, players, slots, scoring, season, week, stats_cache):
    roster_id = entry.get("roster_id")
    starters = entry.get("starters") or []
    starter_points = list(entry.get("starters_points") or [])
    rows = []
    for index, pid in enumerate(starters):
        slot = slots[index] if index < len(slots) else ""
        points = starter_points[index] if index < len(starter_points) else 0
        info = _sleeper_player(players, pid)
        breakdown = _sleeper_breakdown(pid, season, week, scoring, stats_cache)
        row = {
            "name": info["name"],
            "position": info["position"],
            "slot": slot or info["position"] or "?",
            "points": _round_points(points),
        }
        if breakdown:
            row["breakdown"] = breakdown
        rows.append(row)
    return {
        "team": owners.get(roster_id, f"roster {roster_id}"),
        "score": _round_points(entry.get("points")),
        "starters": rows,
    }


def _sleeper_breakdown(pid, season, week, scoring, stats_cache):
    """Score one starter from Sleeper's weekly stat line.

    Returns an empty list when the player has no stats for that week, and
    ``None`` when the stats endpoint has nothing for the player at all so
    the breakdown can be omitted.
    """
    if pid in ("0", 0, None, ""):
        return []
    data = _sleeper_player_stats(pid, season, stats_cache)
    if not data:
        return None
    entries = data.get(str(week))
    if not entries:
        return []
    if isinstance(entries, dict):
        entries = [entries]
    totals = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key, value in (entry.get("stats") or {}).items():
            weight = _scoring_weight(scoring, key)
            if (
                weight is None
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
            ):
                continue
            points = weight * value
            if key not in totals:
                totals[key] = [0.0, weight, 0.0]
            totals[key][0] += value
            totals[key][2] += points
    rows = [
        _breakdown_row(key, value, weight, points)
        for key, (value, weight, points) in totals.items()
        if points
    ]
    rows.sort(key=lambda row: abs(row["points"]), reverse=True)
    return rows


def _sleeper_player_stats(pid, season, stats_cache):
    key = str(pid)
    if key in stats_cache:
        return stats_cache[key]
    try:
        data = _sleeper_get(
            f"/stats/nfl/player/{pid}"
            f"?season_type=regular&season={season}&grouping=week"
        )
    except LeagueLookupError:
        data = None
    if not isinstance(data, dict):
        data = None
    stats_cache[key] = data
    return data


def _matchup_record(matchup_id, home, away):
    if home is None and away is not None:
        home, away = away, None
    is_bye = away is None or home is None
    winner = None
    if home and away and not is_bye:
        if home["score"] > away["score"]:
            winner = home["team"]
        elif away["score"] > home["score"]:
            winner = away["team"]
    return {
        "matchup_id": matchup_id,
        "home": home,
        "away": away,
        "winner": winner,
        "is_bye": is_bye,
    }


def _matchup_id_sort(matchup_id):
    if isinstance(matchup_id, int):
        return (0, matchup_id)
    return (1, 0)


def _breakdown_row(stat, value, weight, points):
    return {
        "stat": _stat_label(stat),
        "value": _compact_number(value),
        "weight": _compact_number(weight),
        "points": round(float(points), 2),
    }


def _stat_label(key):
    if key in _STAT_LABELS:
        return _STAT_LABELS[key]
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(key))
    text = text.replace("_", " ").strip()
    if not text:
        return str(key)
    return text[0].upper() + text[1:]


def _scoring_weight(scoring, key):
    raw = scoring.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _round_points(value):
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _compact_number(value):
    try:
        number = round(float(value), 4)
    except (TypeError, ValueError):
        return 0
    if number.is_integer():
        return int(number)
    return number


def _open_espn(league_id, *, espn_s2="", swid="", year=None):
    from espn_api.football import League as EspnLeague

    try:
        numeric_id = int(league_id)
    except (TypeError, ValueError) as exc:
        raise LeagueLookupError("ESPN league IDs are numeric.") from exc

    season = year or date.today().year
    cookies = {}
    if espn_s2 and swid:
        cookies = {"espn_s2": espn_s2, "swid": swid}

    try:
        return EspnLeague(league_id=numeric_id, year=season, **cookies)
    except Exception as exc:
        message = str(exc).strip() or "ESPN denied the request."
        hint = ""
        if not cookies:
            hint = (
                " If this league is private, install the browser extension, "
                "copy espn_s2 and SWID, and mark the league as private."
            )
        raise LeagueLookupError(
            f"Could not load that ESPN league: {message}.{hint}"
        ) from exc


def _espn_rosters(league):
    remote = _open_espn(
        league.league_id, espn_s2=league.espn_s2, swid=league.swid
    )
    teams = []
    for team in remote.teams or []:
        players = []
        for player in team.roster or []:
            players.append(
                {
                    "name": getattr(player, "name", None) or "?",
                    "position": getattr(player, "position", None) or "?",
                    "nfl_team": getattr(player, "proTeam", None) or "",
                    "acquisition": getattr(player, "acquisitionType", None) or "",
                }
            )
        teams.append({"team": team.team_name, "players": players})
    teams.sort(key=lambda row: row["team"].lower())
    return teams


def _espn_activities(league, since=None):
    from espn_api.requests.espn_requests import ESPNAccessDenied

    remote = _open_espn(
        league.league_id, espn_s2=league.espn_s2, swid=league.swid
    )
    denied = (
        "ESPN denied access to the activity feed. Private leagues need "
        "espn_s2 and SWID from the browser extension."
    )
    rows = []
    offset = 0
    page_size = 25
    while offset < 1000:
        try:
            activities = remote.recent_activity(size=page_size, offset=offset)
        except ESPNAccessDenied:
            return rows, denied
        except Exception as exc:
            return rows, f"Could not load ESPN activity: {exc}"
        if not activities:
            break
        stop = False
        for activity in activities:
            occurred = _epoch_ms_to_datetime(getattr(activity, "date", None))
            if since and occurred and occurred <= since:
                stop = True
                break
            when = _format_epoch_ms(getattr(activity, "date", None))
            for action_tuple in getattr(activity, "actions", []) or []:
                team = action_tuple[0]
                action = action_tuple[1]
                player = action_tuple[2]
                bid = action_tuple[3] if len(action_tuple) > 3 else 0
                rows.append(
                    {
                        "date": when,
                        "team": _espn_team_name(team),
                        "action": action or "?",
                        "player": _espn_player_name(player),
                        "bid": float(bid) if bid else None,
                    }
                )
        if stop or len(activities) < page_size:
            break
        offset += len(activities)
    return rows, None


def _espn_team_name(team):
    if team is None or team == "":
        return "(free agency)"
    return getattr(team, "team_name", None) or str(team)


def _espn_player_name(player):
    if player is None:
        return "?"
    name = getattr(player, "name", None)
    return name or str(player)


def _sleeper_get(path):
    url = f"{SLEEPER_API}{path}"
    try:
        response = requests.get(url, timeout=20)
    except requests.RequestException as exc:
        raise LeagueLookupError(f"Could not reach Sleeper: {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise LeagueLookupError(
            f"Sleeper returned HTTP {response.status_code}."
        )
    try:
        return response.json()
    except ValueError as exc:
        raise LeagueLookupError("Sleeper returned a response that was not JSON.") from exc


def _sleeper_players():
    global _SLEEPER_PLAYERS
    if _SLEEPER_PLAYERS is None:
        path = Path(settings.BASE_DIR) / "apps" / "leagues" / "data" / "nfl_players.json"
        try:
            with path.open() as handle:
                _SLEEPER_PLAYERS = json.load(handle)
        except OSError as exc:
            raise LeagueLookupError(
                "Sleeper player names are unavailable "
                f"(missing {path.name})."
            ) from exc
    return _SLEEPER_PLAYERS


def _sleeper_player(players, pid):
    if pid in ("0", 0, None, ""):
        return {"name": "(empty slot)", "position": "", "nfl_team": ""}
    record = players.get(str(pid)) or {}
    if record:
        name = record.get("full_name") or (
            f"{record.get('first_name', '?')} {record.get('last_name', '?')}"
        )
        return {
            "name": name,
            "position": record.get("position") or "?",
            "nfl_team": record.get("team") or "",
        }
    if isinstance(pid, str) and pid.isalpha():
        return {"name": f"{pid} DEF", "position": "DEF", "nfl_team": pid}
    return {"name": f"player {pid}", "position": "?", "nfl_team": ""}


def _sleeper_roster_context(league_id):
    users = _sleeper_get(f"/v1/league/{league_id}/users") or []
    rosters = _sleeper_get(f"/v1/league/{league_id}/rosters") or []
    display_names = {
        user["user_id"]: (user.get("display_name") or user["user_id"])
        for user in users
    }
    owners = {
        roster["roster_id"]: display_names.get(roster.get("owner_id"), "(unowned)")
        for roster in rosters
    }
    return rosters, owners


def _sleeper_rosters(league_id):
    rosters, owners = _sleeper_roster_context(league_id)
    players = _sleeper_players()
    teams = []
    for roster in rosters:
        ordered_ids = []
        seen = set()
        for pid in (roster.get("starters") or []) + (roster.get("players") or []):
            if pid in ("0", 0, None, "") or pid in seen:
                continue
            seen.add(pid)
            ordered_ids.append(pid)
        rows = []
        for pid in ordered_ids:
            row = _sleeper_player(players, pid)
            row["acquisition"] = ""
            rows.append(row)
        teams.append(
            {
                "team": owners.get(roster["roster_id"], "(unowned)"),
                "players": rows,
            }
        )
    teams.sort(key=lambda row: row["team"].lower())
    return teams


def _sleeper_activities(league_id, since=None):
    info = _sleeper_get(f"/v1/league/{league_id}") or {}
    leg = int((info.get("settings") or {}).get("leg") or 1)
    _, owners = _sleeper_roster_context(league_id)
    players = _sleeper_players()
    rows = []

    if since is None:
        weeks = range(1, leg + 1)
        for week in weeks:
            transactions = _sleeper_get(f"/v1/league/{league_id}/transactions/{week}") or []
            rows.extend(_sleeper_txn_rows(transactions, owners, players, since=None))
    else:
        week = leg
        while week >= 1:
            transactions = _sleeper_get(f"/v1/league/{league_id}/transactions/{week}") or []
            if not isinstance(transactions, list) or not transactions:
                week -= 1
                continue
            latest = max(
                (
                    _epoch_ms_to_datetime(txn.get("created"))
                    or datetime.min.replace(tzinfo=dt_timezone.utc)
                    for txn in transactions
                ),
                default=None,
            )
            rows.extend(_sleeper_txn_rows(transactions, owners, players, since=since))
            if latest and latest <= since:
                break
            week -= 1

    rows.sort(key=lambda row: row.get("date") or "", reverse=True)
    return rows


def _sleeper_txn_rows(transactions, owners, players, since):
    if not isinstance(transactions, list):
        return []

    def owner(roster_id):
        return owners.get(roster_id, f"roster {roster_id}")

    def player_name(pid):
        return _sleeper_player(players, pid)["name"]

    rows = []
    for txn in transactions:
        if txn.get("status") not in (None, "complete"):
            continue
        kind = txn.get("type")
        if kind not in {"waiver", "free_agent", "trade"}:
            continue
        occurred = _epoch_ms_to_datetime(txn.get("created"))
        if since and occurred and occurred <= since:
            continue
        when = _format_epoch_ms(txn.get("created"))
        adds = txn.get("adds") or {}
        drops = txn.get("drops") or {}
        bid = (txn.get("settings") or {}).get("waiver_bid")
        if kind == "trade":
            for pid, roster_id in adds.items():
                rows.append(_activity(when, owner(roster_id), "TRADED", player_name(pid)))
            for pid, roster_id in drops.items():
                rows.append(
                    _activity(when, owner(roster_id), "TRADED AWAY", player_name(pid))
                )
        else:
            action = "WAIVER" if kind == "waiver" else "FA ADDED"
            waiver_bid = float(bid) if kind == "waiver" and bid else None
            for pid, roster_id in adds.items():
                rows.append(
                    _activity(when, owner(roster_id), action, player_name(pid), waiver_bid)
                )
            for pid, roster_id in drops.items():
                rows.append(_activity(when, owner(roster_id), "DROPPED", player_name(pid)))
    return rows


def _existing_snapshot(league):
    try:
        return league.snapshot
    except LeagueSnapshot.DoesNotExist:
        return None


def _merge_activities(new_rows, existing_rows):
    merged = []
    seen = set()
    for row in list(new_rows) + list(existing_rows):
        key = (
            row.get("date"),
            row.get("team"),
            row.get("action"),
            row.get("player"),
            row.get("bid"),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    merged.sort(key=lambda row: row.get("date") or "", reverse=True)
    return merged


def _activity(when, team, action, player, bid=None):
    return {
        "date": when,
        "team": team,
        "action": action,
        "player": player,
        "bid": bid,
    }


def _epoch_ms_to_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=dt_timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _format_epoch_ms(value):
    occurred = _epoch_ms_to_datetime(value)
    if occurred is None:
        return None
    return timezone.localtime(occurred).strftime("%Y-%m-%d %H:%M")
