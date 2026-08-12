import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ffai.byes import load_bye_weeks
from ffai.config import PLAYER_DICT_MAX_AGE_HOURS, SLEEPER_USERNAME
from ffai.player_pool import build_projection, is_draftable
from ffai.projections import ProjectionsAdapter
from ffai.repository import (
    fetch_league,
    fetch_matchups,
    fetch_nfl_state,
    fetch_players,
    fetch_projections,
    fetch_rosters,
    fetch_user,
    fetch_users,
)
from ffai.roster import find_display_name, find_my_roster, find_opponent_roster
from ffai.sleeper_client import SleeperAPIError

logger = logging.getLogger(__name__)


def _stale_note(label, fetched_at):
    """PRD R1: an unmissable, ACCURATE staleness note -- states the actual
    data age, not a placeholder."""
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
        hours = age.total_seconds() / 3600
        if hours < 1:
            age_str = f"{max(int(age.total_seconds() / 60), 0)} minutes old"
        elif hours < 48:
            age_str = f"{hours:.1f} hours old"
        else:
            age_str = f"{hours / 24:.1f} days old"
    except (TypeError, ValueError):
        age_str = "age unknown"
    return f"{label} is stale -- {age_str} (cached {fetched_at})"


@dataclass
class WeeklyContext:
    week: "int | None"
    season: "str | None"
    league: dict
    players_raw: dict  # full Sleeper player dictionary -- for injury_status/depth_chart lookups in brief.py
    roster_positions: list
    my_roster: "dict | None"
    my_players: list  # list[PlayerProjection], rostered players (unfiltered by is_draftable)
    opponent_roster: "dict | None"
    opponent_display_name: "str | None"
    opponent_players: list  # list[PlayerProjection]
    projections_degraded: bool
    warnings: list = field(default_factory=list)  # staleness + degradation notes, for the R1 banner
    rosters: list = field(default_factory=list)  # every roster in the league -- waivers.py needs this to find free agents
    projections: "object | None" = None  # the raw ProjectionsResult -- waivers.py needs this to score free agents


def gather_weekly_context(
    client,
    cache,
    league_id,
    week=None,
    username=SLEEPER_USERNAME,
    adapter=None,
    player_dict_max_age_hours=PLAYER_DICT_MAX_AGE_HOURS,
):
    """Composition root for brief/startsit/refresh: gathers everything those
    commands need in one place so they can't drift out of sync. Every fetch
    follows repository.py's last-known-good pattern; any staleness or
    degradation is collected into `warnings` for the caller to surface (PRD
    R1 -- an unmissable banner, not a silent fallback).

    `player_dict_max_age_hours=0` forces a live re-pull of the player
    dictionary regardless of the normal once-daily cache policy -- used by
    the `refresh` command (R7: bypass all caches)."""
    adapter = adapter or ProjectionsAdapter()
    warnings = []

    league, league_stale, league_fetched_at = fetch_league(client, cache, league_id)
    if league_stale:
        warnings.append(_stale_note("league data", league_fetched_at))

    if week is None:
        state, state_stale, state_fetched_at = fetch_nfl_state(client, cache)
        week = state.get("week")
        if state_stale:
            warnings.append(_stale_note("NFL week/state", state_fetched_at))

    players_raw, players_stale, players_fetched_at = fetch_players(
        client, cache, max_age_hours=player_dict_max_age_hours
    )
    if players_stale:
        warnings.append(_stale_note("player dictionary", players_fetched_at))

    rosters, rosters_stale, rosters_fetched_at = fetch_rosters(client, cache, league_id)
    if rosters_stale:
        warnings.append(_stale_note("rosters", rosters_fetched_at))

    users, users_stale, users_fetched_at = fetch_users(client, cache, league_id)
    if users_stale:
        warnings.append(_stale_note("league users", users_fetched_at))

    user, user_stale, user_fetched_at = fetch_user(client, cache, username)
    if user_stale:
        warnings.append(_stale_note("user lookup", user_fetched_at))

    try:
        matchups, matchups_stale, matchups_fetched_at = fetch_matchups(client, cache, league_id, week)
        if matchups_stale:
            warnings.append(_stale_note("matchups", matchups_fetched_at))
    except SleeperAPIError as exc:
        # Expected before the league's schedule starts (e.g. preseason) --
        # no matchup data yet is a normal degraded case (R2), not an error.
        logger.info("no matchup data available for week %s: %s", week, exc)
        matchups = []

    scoring_settings = league.get("scoring_settings", {})
    roster_positions = league.get("roster_positions", [])
    season = league.get("season")

    known_player_ids = {pid for pid, p in players_raw.items() if is_draftable(p)}
    projections, proj_stale, _ = fetch_projections(
        adapter, cache, players_raw, season, week=week, known_player_ids=known_player_ids
    )
    if proj_stale or projections.degraded:
        warnings.append(f"projections degraded/stale ({'; '.join(projections.warnings)})")

    byes_by_team = load_bye_weeks()

    my_roster = find_my_roster(rosters, user.get("user_id"))
    my_players = _roster_projections(my_roster, players_raw, projections, scoring_settings, byes_by_team)

    opponent_roster = None
    if my_roster is not None:
        opponent_roster = find_opponent_roster(matchups, my_roster["roster_id"], rosters)
    opponent_players = _roster_projections(opponent_roster, players_raw, projections, scoring_settings, byes_by_team)
    opponent_display_name = (
        find_display_name(users, opponent_roster.get("owner_id")) if opponent_roster is not None else None
    )

    return WeeklyContext(
        week=week,
        season=season,
        league=league,
        players_raw=players_raw,
        roster_positions=roster_positions,
        my_roster=my_roster,
        my_players=my_players,
        opponent_roster=opponent_roster,
        opponent_display_name=opponent_display_name,
        opponent_players=opponent_players,
        projections_degraded=projections.degraded,
        warnings=warnings,
        rosters=rosters,
        projections=projections,
    )


def _roster_projections(roster, players_raw, projections, scoring_settings, byes_by_team):
    if roster is None:
        return []
    result = []
    for player_id in roster.get("players") or []:
        projection = build_projection(player_id, players_raw, projections, scoring_settings, byes_by_team)
        if projection is not None:
            result.append(projection)
    return result
