import argparse
import sys
import time
from pathlib import Path

from ffai.brief import render_brief_markdown
from ffai.byes import load_bye_weeks
from ffai.cache import Cache
from ffai.config import CACHE_DB_PATH, DRAFT_POLL_INTERVAL_SECONDS, LEAGUE_ID
from ffai.draft_assistant import DraftAssistant, format_recommendation
from ffai.lineup import format_lineup, optimize_lineup
from ffai.player_pool import is_draftable, to_projection_pool
from ffai.projections import ProjectionsAdapter
from ffai.repository import fetch_league, fetch_players, fetch_projections
from ffai.sleeper_client import SleeperClient
from ffai.vorp import build_tiers, compute_replacement_levels, compute_vorp
from ffai.weekly_context import gather_weekly_context


def cmd_draft(args):
    client = SleeperClient()
    cache = Cache(db_path=CACHE_DB_PATH)
    adapter = ProjectionsAdapter()

    league, league_stale, _ = fetch_league(client, cache, args.league_id)
    if league_stale:
        print("WARNING: using cached league data (live fetch failed)")

    players_raw, players_stale, _ = fetch_players(client, cache)
    if players_stale:
        print("WARNING: using cached player dictionary (live fetch failed)")

    scoring_settings = league.get("scoring_settings", {})
    roster_positions = league.get("roster_positions", [])
    total_rosters = league.get("total_rosters")
    season = league.get("season")

    known_player_ids = {pid for pid, p in players_raw.items() if is_draftable(p)}
    projections, proj_stale, _ = fetch_projections(
        adapter, cache, players_raw, season, known_player_ids=known_player_ids
    )
    if proj_stale or projections.degraded:
        print(f"WARNING: projections degraded/stale ({'; '.join(projections.warnings)})")

    byes_by_team = load_bye_weeks()
    pool = to_projection_pool(players_raw, projections, scoring_settings, byes_by_team=byes_by_team)
    replacement_levels = compute_replacement_levels(pool, roster_positions, total_rosters)
    board = compute_vorp(pool, replacement_levels)
    build_tiers(board)  # mutates each player's .tier in place

    consensus_top10 = adapter.fetch_consensus_top10(players_raw)

    assistant = DraftAssistant(
        client,
        cache,
        args.draft_id,
        board,
        roster_positions,
        args.draft_slot,
        board_degraded=projections.degraded,
        consensus_top10=consensus_top10,
    )

    print(f"Draft assistant started for draft {args.draft_id} "
          f"(polling every {DRAFT_POLL_INTERVAL_SECONDS}s, Ctrl+C to stop)")
    print(format_recommendation(assistant.recommend()))

    try:
        while True:
            time.sleep(DRAFT_POLL_INTERVAL_SECONDS)
            new_picks = assistant.poll_once()
            if not new_picks:
                continue
            for pick in new_picks:
                player = assistant._by_player_id.get(pick.get("player_id"))
                name = player.name if player else pick.get("player_id")
                print(f"Pick {pick.get('pick_no')}: {name}")
            print(format_recommendation(assistant.recommend()))
    except KeyboardInterrupt:
        print("Stopped.")

    return 0


def _format_startsit_text(context):
    if context.my_roster is None:
        return "No roster found for you in this league yet."

    lines = []
    if context.projections_degraded:
        lines.append("NO PROJECTIONS AVAILABLE -- showing your currently-set Sleeper lineup only, not optimized.")
        starter_ids = [pid for pid in context.my_roster.get("starters") or [] if pid != "0"]
        by_id = {p.player_id: p for p in context.my_players}
        if not starter_ids:
            lines.append("  (no lineup set yet)")
        for player_id in starter_ids:
            p = by_id.get(player_id)
            lines.append(f"  {p.name} ({p.position}, {p.team})" if p else f"  (unknown player {player_id})")
    else:
        result = optimize_lineup(context.my_players, context.roster_positions)
        lines.append(format_lineup(result))

    if context.warnings:
        lines.append("")
        lines.append("WARNINGS:")
        for warning in context.warnings:
            lines.append(f"  - {warning}")

    return "\n".join(lines)


def cmd_startsit(args):
    client = SleeperClient()
    cache = Cache(db_path=CACHE_DB_PATH)

    context = gather_weekly_context(client, cache, args.league_id, week=args.week)
    print(_format_startsit_text(context))
    return 0


def cmd_brief(args):
    client = SleeperClient()
    cache = Cache(db_path=CACHE_DB_PATH)

    context = gather_weekly_context(client, cache, args.league_id, week=args.week)
    markdown = render_brief_markdown(context)

    if args.out:
        Path(args.out).write_text(markdown, encoding="utf-8")
        print(f"Brief written to {args.out}")
    else:
        print(markdown)
    return 0


def cmd_refresh(args):
    """R7: the escape hatch. Forces a fresh pull bypassing the player
    dictionary's normal once-daily cache policy, and prints a plain-text
    start/sit summary -- the closest existing equivalent to "save the bad
    Tuesday" until waivers ship in Phase 3, at which point this extends to
    include a waiver summary too."""
    client = SleeperClient()
    cache = Cache(db_path=CACHE_DB_PATH)

    context = gather_weekly_context(client, cache, args.league_id, week=args.week, player_dict_max_age_hours=0)
    print(_format_startsit_text(context))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="ffai")
    subparsers = parser.add_subparsers(dest="command", required=True)

    draft_parser = subparsers.add_parser("draft", help="Live draft assistant")
    draft_parser.add_argument("draft_id", help="Sleeper draft_id to monitor")
    draft_parser.add_argument("draft_slot", help="Your draft_slot (draft position, 1-N) within this draft")
    draft_parser.add_argument("--league-id", default=LEAGUE_ID, help="League ID (default: configured league)")
    draft_parser.set_defaults(func=cmd_draft)

    startsit_parser = subparsers.add_parser("startsit", help="Lineup recommendation for the current week")
    startsit_parser.add_argument("--league-id", default=LEAGUE_ID, help="League ID (default: configured league)")
    startsit_parser.add_argument("--week", type=int, default=None, help="Week override (default: auto-detected)")
    startsit_parser.set_defaults(func=cmd_startsit)

    brief_parser = subparsers.add_parser("brief", help="Generate the weekly markdown brief")
    brief_parser.add_argument("--league-id", default=LEAGUE_ID, help="League ID (default: configured league)")
    brief_parser.add_argument("--week", type=int, default=None, help="Week override (default: auto-detected)")
    brief_parser.add_argument("--out", default=None, help="Write the brief to this file instead of stdout")
    brief_parser.set_defaults(func=cmd_brief)

    refresh_parser = subparsers.add_parser("refresh", help="Escape hatch: force a fresh pull, print start/sit summary")
    refresh_parser.add_argument("--league-id", default=LEAGUE_ID, help="League ID (default: configured league)")
    refresh_parser.add_argument("--week", type=int, default=None, help="Week override (default: auto-detected)")
    refresh_parser.set_defaults(func=cmd_refresh)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
