"""Replay a completed season through the in-season code paths.

Why this exists: the draft assistant needed six bugs' worth of
troubleshooting, and every one of them was found the same way -- replay a
real draft and look at what the tool actually recommended. The in-season
code (lineup optimisation, the weekly brief, waivers) has had none of that.
Worse, it has only ever run against an EMPTY roster: every scheduled brief
so far executed cleanly because the league had not drafted yet, so each
function correctly returned "nothing to do". Week 1 would be the first time
any of it saw real players.

The predecessor league (PRD Open Question #4: same managers, same 10-team
PPR settings, same roster shape) played a full 2025 season, and Sleeper
still serves all of it. That gives this harness something backtest.py never
had: GROUND TRUTH. The draft could only ever grade our projections using
our own projections, which is why every number it produced came with a
circularity caveat. Here the question has a real answer -- would the
lineup the tool recommends have outscored what the manager actually
started? -- graded on points that really happened.

Three things are measured per manager-week:

  tool vs human    the decision that matters. Both lineups are scored on
                   Sleeper's own per-player actuals for that week, so the
                   comparison is free of our scoring or projection code.
  tool vs perfect  points left on the bench against hindsight -- the PRD's
                   headline metric. Nobody hits 0; it is a ceiling.
  slot validity    did optimize_lineup return a legal, filled lineup. A
                   crash or an empty slot here is a bug, not a bad call.

Deliberately uses each week's matchup entry rather than /rosters for the
roster: /rosters returns the FINAL roster, so replaying week 3 with it
would hand the tool players the manager had not acquired yet. The matchup
entry's `players` is the roster as it stood that week.

Usage:  python season_replay.py [--weeks 1-14] [--league <id>] [--verbose]
"""
import argparse
import sys
from collections import defaultdict

from ffai.byes import load_bye_weeks
from ffai.cache import Cache
from ffai.lineup import optimize_lineup
from ffai.player_pool import build_projection, is_draftable
from ffai.projections import ProjectionsAdapter
from ffai.repository import fetch_league, fetch_players, fetch_projections
from ffai.roster import find_display_name
from ffai.sleeper_client import SleeperAPIError, SleeperClient

# The 2025 season of this league's manager group, confirmed in CLAUDE.md as
# the same 10-team PPR format with the same roster shape.
PREVIOUS_LEAGUE_ID = "1261164996717453313"


def parse_weeks(spec):
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(w) for w in spec.split(",")]


def _replace_points(player, points):
    """A copy of a PlayerProjection carrying `points` instead of its
    projection -- used to build the hindsight lineup, so the SAME
    optimize_lineup runs over actuals and the ceiling is computed by the
    code under test rather than by a second implementation."""
    from dataclasses import replace

    return replace(player, points=points)


def replay_week(client, cache, adapter, league, players_raw, week, byes, verbose=False):
    """One week, every manager. Returns a list of per-manager result dicts."""
    league_id = league["league_id"]
    scoring = league.get("scoring_settings", {})
    roster_positions = league.get("roster_positions", [])
    season = league.get("season")

    try:
        matchups = client.get_matchups(league_id, week)
    except SleeperAPIError as exc:
        print(f"  week {week}: no matchup data ({exc})")
        return []

    known = {pid for pid, p in players_raw.items() if is_draftable(p)}
    projections, proj_stale, _fetched = fetch_projections(
        adapter, cache, players_raw, season, week=week, known_player_ids=known
    )
    if projections.degraded:
        print(f"  week {week}: PROJECTIONS DEGRADED ({'; '.join(projections.warnings)}) -- skipping")
        return []

    results = []
    for entry in matchups:
        roster_player_ids = entry.get("players") or []
        actuals = entry.get("players_points") or {}
        human_starters = [pid for pid in (entry.get("starters") or []) if pid and pid != "0"]
        if not roster_player_ids or not actuals:
            continue

        # What the tool would have seen that week.
        projected = []
        missing_projection = 0
        for pid in roster_player_ids:
            p = build_projection(pid, players_raw, projections, scoring, byes_by_team=byes)
            if p is None:
                continue
            if p.data_source == "none":
                missing_projection += 1
            projected.append(p)

        tool_lineup = optimize_lineup(projected, roster_positions)
        tool_starters = [s.player.player_id for s in tool_lineup.slots if s.player is not None]
        empty_slots = [s.slot for s in tool_lineup.slots if s.player is None]

        # Ground truth: Sleeper's own per-player actuals under this league's
        # scoring. Neither our projection code nor our scoring code touches
        # this side of the comparison.
        def actual_total(player_ids):
            return round(sum(actuals.get(pid, 0.0) for pid in player_ids), 2)

        tool_points = actual_total(tool_starters)
        human_points = actual_total(human_starters)

        hindsight = optimize_lineup(
            [_replace_points(p, actuals.get(p.player_id, 0.0)) for p in projected], roster_positions
        )

        results.append({
            "week": week,
            "roster_id": entry.get("roster_id"),
            "tool": tool_points,
            "human": human_points,
            "perfect": round(hindsight.total_points, 2),
            "empty_slots": empty_slots,
            "missing_projection": missing_projection,
            "roster_size": len(projected),
        })

        if verbose:
            print(f"    wk{week} roster {entry.get('roster_id')}: "
                  f"tool {tool_points:6.2f}  human {human_points:6.2f}  perfect {hindsight.total_points:6.2f}"
                  + (f"  EMPTY {empty_slots}" if empty_slots else ""))

    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--league", default=PREVIOUS_LEAGUE_ID, help="league to replay (default: the 2025 predecessor)")
    parser.add_argument("--weeks", default=None, help="e.g. 1-14 or 1,5,9 (default: the regular season)")
    parser.add_argument("--verbose", action="store_true", help="print every manager-week")
    args = parser.parse_args()

    client = SleeperClient()
    cache = Cache()
    adapter = ProjectionsAdapter()

    league, _stale, _at = fetch_league(client, cache, args.league)
    players_raw, _s, _a = fetch_players(client, cache)
    byes = load_bye_weeks()

    playoff_start = (league.get("settings") or {}).get("playoff_week_start") or 15
    weeks = parse_weeks(args.weeks) if args.weeks else list(range(1, playoff_start))

    print(f"Replaying {league.get('name')} {league.get('season')} "
          f"({league.get('total_rosters')} teams, roster {league.get('roster_positions')})")
    print(f"Weeks {weeks[0]}-{weeks[-1]}  (playoffs start week {playoff_start})\n")

    all_results = []
    for week in weeks:
        rows = replay_week(client, cache, adapter, league, players_raw, week, byes, verbose=args.verbose)
        all_results.extend(rows)
        if rows and not args.verbose:
            wins = sum(1 for r in rows if r["tool"] > r["human"])
            delta = sum(r["tool"] - r["human"] for r in rows) / len(rows)
            print(f"  week {week:2d}: {len(rows)} managers | tool beat human {wins}/{len(rows)} | "
                  f"avg {delta:+6.2f} pts")

    if not all_results:
        print("\nNo results -- nothing to report.")
        return 1

    n = len(all_results)
    wins = sum(1 for r in all_results if r["tool"] > r["human"])
    ties = sum(1 for r in all_results if abs(r["tool"] - r["human"]) < 0.01)
    avg_delta = sum(r["tool"] - r["human"] for r in all_results) / n
    avg_tool = sum(r["tool"] for r in all_results) / n
    avg_human = sum(r["human"] for r in all_results) / n
    avg_perfect = sum(r["perfect"] for r in all_results) / n
    tool_left = sum(r["perfect"] - r["tool"] for r in all_results) / n
    human_left = sum(r["perfect"] - r["human"] for r in all_results) / n

    broken = [r for r in all_results if r["empty_slots"]]
    gaps = sum(r["missing_projection"] for r in all_results)

    print(f"\n=== {n} manager-weeks ===")
    print(f"  tool    {avg_tool:7.2f} pts/wk")
    print(f"  human   {avg_human:7.2f} pts/wk")
    print(f"  perfect {avg_perfect:7.2f} pts/wk   (hindsight ceiling)")
    print(f"\n  tool beat the human in {wins}/{n} ({100*wins/n:.0f}%), {ties} exact ties, avg {avg_delta:+.2f} pts/wk")
    print(f"  points left on the bench -- tool {tool_left:.2f}/wk, human {human_left:.2f}/wk")
    print(f"\n  lineups with an empty slot: {len(broken)}  (any non-zero is a bug, not a bad call)")
    print(f"  rostered players with no projection: {gaps} of {sum(r['roster_size'] for r in all_results)}")

    if broken:
        for r in broken[:5]:
            print(f"    wk{r['week']} roster {r['roster_id']}: {r['empty_slots']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
