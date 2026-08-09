import argparse
import sys
import time

from ffai.byes import load_bye_weeks
from ffai.cache import Cache
from ffai.config import CACHE_DB_PATH, DRAFT_POLL_INTERVAL_SECONDS, LEAGUE_ID
from ffai.draft_assistant import DraftAssistant, format_recommendation
from ffai.player_pool import is_draftable, to_projection_pool
from ffai.projections import ProjectionsAdapter
from ffai.repository import fetch_league, fetch_players, fetch_projections
from ffai.sleeper_client import SleeperClient
from ffai.vorp import build_tiers, compute_replacement_levels, compute_vorp


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


def build_parser():
    parser = argparse.ArgumentParser(prog="ffai")
    subparsers = parser.add_subparsers(dest="command", required=True)

    draft_parser = subparsers.add_parser("draft", help="Live draft assistant")
    draft_parser.add_argument("draft_id", help="Sleeper draft_id to monitor")
    draft_parser.add_argument("draft_slot", help="Your draft_slot (draft position, 1-N) within this draft")
    draft_parser.add_argument("--league-id", default=LEAGUE_ID, help="League ID (default: configured league)")
    draft_parser.set_defaults(func=cmd_draft)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
