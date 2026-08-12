from types import SimpleNamespace

from ffai.lineup import optimize_lineup
from ffai.models import BenchPointsLostResult
from ffai.player_pool import build_projection
from ffai.repository import fetch_matchups, fetch_stats
from ffai.sleeper_client import SleeperAPIError


def compute_bench_points_lost(matchup, week, players_raw, actual_stats_by_player, scoring_settings, roster_positions, byes_by_team=None):
    """PRD's Tier 2 headline metric: the weekly delta between what you
    actually started and the retrospectively optimal lineup, "computed
    exactly from Sleeper data, no judgment involved." Reuses
    lineup.optimize_lineup unmodified -- fed real final stats instead of
    projections, via a lightweight stand-in (`actual_as_projections`) that
    gives player_pool.build_projection the same .raw_stats_by_player/.source
    interface a real ProjectionsResult has, without needing an actual
    projections fetch.

    `matchup` is this roster's entry from GET /league/{id}/matchups/{week}
    -- its "players" field is the full roster as it stood that week (the
    right pool for "what could you have started," including anyone since
    traded/dropped) and "starters" is what was actually started."""
    roster_player_ids = matchup.get("players") or []
    actual_as_projections = SimpleNamespace(raw_stats_by_player=actual_stats_by_player, source="actual")

    players = []
    for player_id in roster_player_ids:
        projection = build_projection(player_id, players_raw, actual_as_projections, scoring_settings, byes_by_team)
        if projection is not None:
            players.append(projection)
    by_id = {p.player_id: p for p in players}

    optimal = optimize_lineup(players, roster_positions)

    starter_ids = {pid for pid in matchup.get("starters") or [] if pid != "0"}
    actual_points = matchup.get("points")
    if actual_points is None:
        actual_points = sum(by_id[pid].points for pid in starter_ids if pid in by_id)
    actual_points = round(actual_points, 2)

    optimal_starter_ids = {s.player.player_id for s in optimal.slots if s.player is not None}
    swapped_in = [by_id[pid] for pid in (optimal_starter_ids - starter_ids) if pid in by_id]
    swapped_out = [by_id[pid] for pid in (starter_ids - optimal_starter_ids) if pid in by_id]

    return BenchPointsLostResult(
        week=week,
        actual_points=actual_points,
        optimal_points=optimal.total_points,
        bench_points_lost=round(optimal.total_points - actual_points, 2),
        swapped_in=swapped_in,
        swapped_out=swapped_out,
    )


def compute_season_bench_report(
    client, cache, league_id, my_roster_id, players_raw, scoring_settings, roster_positions, season,
    start_week, end_week, byes_by_team=None,
):
    """Runs compute_bench_points_lost for every week in [start_week,
    end_week] -- the season-long trend PRD's Tier 2 headline metric needs
    ("trending down over the season means the start/sit engine is earning
    its keep"). A week with no matchup data, no roster snapshot, or no
    final stats yet is skipped with a reason, not an error (R2) -- normal
    for the current/future weeks, not a failure."""
    results = []
    skipped = []
    for week in range(start_week, end_week + 1):
        try:
            matchups, _, _ = fetch_matchups(client, cache, league_id, week)
        except SleeperAPIError:
            skipped.append((week, "no matchup data available"))
            continue

        matchup = next((m for m in matchups if m.get("roster_id") == my_roster_id), None)
        if matchup is None or not matchup.get("players"):
            skipped.append((week, "no roster/matchup data for this week yet"))
            continue

        try:
            actual_stats, _, _ = fetch_stats(client, cache, season, week)
        except SleeperAPIError:
            skipped.append((week, "no final stats available for this week yet"))
            continue

        results.append(
            compute_bench_points_lost(
                matchup, week, players_raw, actual_stats, scoring_settings, roster_positions, byes_by_team
            )
        )
    return results, skipped


def format_bench_report(results, skipped):
    if not results:
        lines = ["No completed weeks with data yet."]
    else:
        lines = ["Bench points lost by week:"]
        for r in results:
            lines.append(
                f"  Week {r.week}: started {r.actual_points:.1f} pts, optimal was {r.optimal_points:.1f} pts "
                f"-- lost {r.bench_points_lost:.1f} pts"
            )
            for p in r.swapped_out:
                lines.append(f"    should have benched: {p.name} ({p.position}) -- {p.points:.1f} pts")
            for p in r.swapped_in:
                lines.append(f"    should have started: {p.name} ({p.position}) -- {p.points:.1f} pts")

        total_lost = round(sum(r.bench_points_lost for r in results), 2)
        avg_lost = round(total_lost / len(results), 2)
        lines.append(f"Total: {total_lost:.1f} pts lost over {len(results)} week(s) (avg {avg_lost:.1f}/week).")

    if skipped:
        lines.append("Skipped: " + "; ".join(f"week {w} ({reason})" for w, reason in skipped))

    return "\n".join(lines)
