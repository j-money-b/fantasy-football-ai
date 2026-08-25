import argparse
import sys
import time
from pathlib import Path

from ffai.brief import render_brief_markdown
from ffai.byes import load_bye_weeks
from ffai.cache import Cache
from ffai.config import CACHE_DB_PATH, DRAFT_POLL_INTERVAL_SECONDS, LEAGUE_ID, SLEEPER_USERNAME
from ffai.draft_assistant import DraftAssistant, format_recommendation
from ffai.lineup import format_lineup, optimize_lineup
from ffai.models import TradeParty
from ffai.player_pool import is_draftable, to_projection_pool
from ffai.projections import ProjectionsAdapter
from ffai.repository import (
    fetch_draft,
    fetch_league,
    fetch_nfl_state,
    fetch_players,
    fetch_projections,
    fetch_rosters,
    fetch_trending_adds,
    fetch_user,
    fetch_users,
)
from ffai.retrospective import compute_season_bench_report, format_bench_report
from ffai.roster import find_display_name, find_my_roster, find_roster_by_display_name
from ffai.sleeper_client import SleeperAPIError, SleeperClient
from ffai.trade import evaluate_trade, format_trade_evaluation, resolve_player_ids
from ffai.vorp import build_tiers, compute_replacement_levels, compute_vorp
from ffai.waivers import (
    compute_free_agents,
    faab_bid_ranges,
    format_waiver_report,
    is_faab,
    priority_judgment,
    rank_waiver_targets,
    waiver_system_label,
    budget_pace_warning,
)
from ffai.weekly_context import gather_weekly_context


def resolve_draft_slot(client, cache, draft_id, username):
    """Auto-detect the user's draft_slot from the draft's own draft_order
    (Sleeper user_id -> slot), keyed by the configured Sleeper username.
    Returns the slot as a string, or None if it can't be determined (order
    not yet set by the commissioner, or this user isn't in it) -- callers
    must fail loudly rather than guess (PRD anti-goal: confidently wrong)."""
    draft, _stale, _fetched_at = fetch_draft(client, cache, draft_id)
    draft_order = draft.get("draft_order")
    if not draft_order:
        return None

    user, _stale, _fetched_at = fetch_user(client, cache, username)
    user_id = user.get("user_id")
    if user_id is None:
        return None

    slot = draft_order.get(str(user_id))
    return str(slot) if slot is not None else None


def cmd_draft(args):
    client = SleeperClient()
    cache = Cache(db_path=CACHE_DB_PATH)
    adapter = ProjectionsAdapter()

    draft_slot = args.draft_slot
    if draft_slot is None:
        draft_slot = resolve_draft_slot(client, cache, args.draft_id, SLEEPER_USERNAME)
        if draft_slot is None:
            print(
                "ERROR: could not auto-detect your draft_slot (draft_order not set yet, "
                "or your Sleeper username isn't in this draft). Pass it explicitly: "
                "ffai draft <draft_id> <draft_slot>"
            )
            return 1
        print(f"Auto-detected draft_slot {draft_slot} for {SLEEPER_USERNAME}")

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
        draft_slot,
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


def cmd_trade(args):
    client = SleeperClient()
    cache = Cache(db_path=CACHE_DB_PATH)
    adapter = ProjectionsAdapter()

    league, league_stale, _ = fetch_league(client, cache, args.league_id)
    if league_stale:
        print("WARNING: using cached league data (live fetch failed)")

    players_raw, players_stale, _ = fetch_players(client, cache)
    if players_stale:
        print("WARNING: using cached player dictionary (live fetch failed)")

    rosters, rosters_stale, _ = fetch_rosters(client, cache, args.league_id)
    if rosters_stale:
        print("WARNING: using cached rosters (live fetch failed)")

    users, users_stale, _ = fetch_users(client, cache, args.league_id)
    if users_stale:
        print("WARNING: using cached league users (live fetch failed)")

    user, user_stale, _ = fetch_user(client, cache, SLEEPER_USERNAME)
    if user_stale:
        print("WARNING: using cached user lookup (live fetch failed)")

    my_roster = find_my_roster(rosters, user.get("user_id"))
    if my_roster is None:
        print("No roster found for you in this league yet -- can't evaluate a trade.")
        return 1

    other_roster = find_roster_by_display_name(rosters, users, args.with_manager)
    if other_roster is None:
        print(f"No manager found matching {args.with_manager!r}.")
        return 1

    try:
        send_ids = resolve_player_ids(args.send, players_raw)
        receive_ids = resolve_player_ids(args.receive, players_raw)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1

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
    board_by_id = {p.player_id: p for p in compute_vorp(pool, replacement_levels)}

    party_a = TradeParty(
        label=find_display_name(users, my_roster.get("owner_id")) or "You",
        roster_player_ids=my_roster.get("players") or [],
        sends_ids=send_ids,
    )
    party_b = TradeParty(
        label=find_display_name(users, other_roster.get("owner_id")) or args.with_manager,
        roster_player_ids=other_roster.get("players") or [],
        sends_ids=receive_ids,
    )

    evaluation = evaluate_trade(party_a, party_b, board_by_id, roster_positions)
    print(format_trade_evaluation(evaluation))
    return 0


def cmd_bench_report(args):
    client = SleeperClient()
    cache = Cache(db_path=CACHE_DB_PATH)

    league, league_stale, _ = fetch_league(client, cache, args.league_id)
    if league_stale:
        print("WARNING: using cached league data (live fetch failed)")

    players_raw, players_stale, _ = fetch_players(client, cache)
    if players_stale:
        print("WARNING: using cached player dictionary (live fetch failed)")

    rosters, rosters_stale, _ = fetch_rosters(client, cache, args.league_id)
    if rosters_stale:
        print("WARNING: using cached rosters (live fetch failed)")

    user, user_stale, _ = fetch_user(client, cache, SLEEPER_USERNAME)
    if user_stale:
        print("WARNING: using cached user lookup (live fetch failed)")

    my_roster = find_my_roster(rosters, user.get("user_id"))
    if my_roster is None:
        print("No roster found for you in this league yet -- can't compute bench points lost.")
        return 1

    end_week = args.end_week
    if end_week is None:
        state, state_stale, _ = fetch_nfl_state(client, cache)
        if state_stale:
            print("WARNING: using cached NFL state (live fetch failed)")
        end_week = max((state.get("week") or 1) - 1, 0)  # last COMPLETED week -- this week isn't final yet

    if end_week < args.start_week:
        print("No completed weeks with data yet.")
        return 0

    scoring_settings = league.get("scoring_settings", {})
    roster_positions = league.get("roster_positions", [])
    season = league.get("season")
    byes_by_team = load_bye_weeks()

    results, skipped = compute_season_bench_report(
        client, cache, args.league_id, my_roster["roster_id"], players_raw,
        scoring_settings, roster_positions, season, args.start_week, end_week, byes_by_team,
    )
    print(format_bench_report(results, skipped))
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


def _format_waivers_text(context, client, cache):
    if context.my_roster is None:
        return "No roster found for you in this league yet -- can't compute waiver targets."

    league_settings = context.league.get("settings", {})
    scoring_settings = context.league.get("scoring_settings", {})
    roster_settings = context.my_roster.get("settings", {}) or {}
    byes_by_team = load_bye_weeks()

    trending_counts = {}
    try:
        trending, _, _ = fetch_trending_adds(client, cache)
        trending_counts = {row["player_id"]: row.get("count") for row in trending if row.get("player_id")}
    except SleeperAPIError:
        pass  # supplementary signal only (PRD 6.3), not load-bearing

    free_agents = compute_free_agents(
        context.players_raw, context.rosters, context.projections, scoring_settings, byes_by_team
    )
    targets = rank_waiver_targets(
        free_agents,
        context.my_players,
        context.roster_positions,
        trending_counts=trending_counts,
        projections_degraded=context.projections_degraded,
    )

    label = waiver_system_label(league_settings)
    faab = is_faab(league_settings)

    lines = []
    if context.projections_degraded:
        lines.append("NO PROJECTIONS AVAILABLE -- waiver targets ranked by roster need and trending-add velocity only.")
        lines.append("")

    if faab:
        remaining = league_settings.get("waiver_budget", 0) - roster_settings.get("waiver_budget_used", 0)
        bids = faab_bid_ranges(targets, remaining)
        pace = budget_pace_warning(league_settings, roster_settings, context.week)
        lines.append(format_waiver_report(targets, label, True, remaining_budget=remaining, faab_bids=bids, pace_warning=pace))
    else:
        priority_note = priority_judgment(targets[0] if targets else None, waiver_position=roster_settings.get("waiver_position"))
        lines.append(format_waiver_report(targets, label, False, priority_note=priority_note))

    if context.warnings:
        lines.append("")
        lines.append("WARNINGS:")
        for warning in context.warnings:
            lines.append(f"  - {warning}")

    return "\n".join(lines)


def cmd_waivers(args):
    client = SleeperClient()
    cache = Cache(db_path=CACHE_DB_PATH)

    context = gather_weekly_context(client, cache, args.league_id, week=args.week)
    print(_format_waivers_text(context, client, cache))
    return 0


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
    start/sit summary plus a waiver summary -- "save the bad Tuesday"."""
    client = SleeperClient()
    cache = Cache(db_path=CACHE_DB_PATH)

    context = gather_weekly_context(client, cache, args.league_id, week=args.week, player_dict_max_age_hours=0)
    print(_format_startsit_text(context))
    print()
    print(_format_waivers_text(context, client, cache))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="ffai")
    subparsers = parser.add_subparsers(dest="command", required=True)

    draft_parser = subparsers.add_parser("draft", help="Live draft assistant")
    draft_parser.add_argument("draft_id", help="Sleeper draft_id to monitor")
    draft_parser.add_argument(
        "draft_slot", nargs="?", default=None,
        help="Your draft_slot (draft position, 1-N) within this draft. "
             "If omitted, auto-detected from the draft's draft_order + your configured Sleeper username.",
    )
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

    refresh_parser = subparsers.add_parser("refresh", help="Escape hatch: force a fresh pull, print start/sit + waiver summary")
    refresh_parser.add_argument("--league-id", default=LEAGUE_ID, help="League ID (default: configured league)")
    refresh_parser.add_argument("--week", type=int, default=None, help="Week override (default: auto-detected)")
    refresh_parser.set_defaults(func=cmd_refresh)

    waivers_parser = subparsers.add_parser("waivers", help="Waiver-wire targets ranked for the current week")
    waivers_parser.add_argument("--league-id", default=LEAGUE_ID, help="League ID (default: configured league)")
    waivers_parser.add_argument("--week", type=int, default=None, help="Week override (default: auto-detected)")
    waivers_parser.set_defaults(func=cmd_waivers)

    trade_parser = subparsers.add_parser("trade", help="Evaluate a proposed trade")
    trade_parser.add_argument("--send", action="append", required=True, help="Player name you'd send away (repeatable)")
    trade_parser.add_argument("--receive", action="append", required=True, help="Player name you'd receive (repeatable)")
    trade_parser.add_argument("--with", dest="with_manager", required=True, help="The other manager's Sleeper display name")
    trade_parser.add_argument("--league-id", default=LEAGUE_ID, help="League ID (default: configured league)")
    trade_parser.set_defaults(func=cmd_trade)

    bench_report_parser = subparsers.add_parser(
        "bench-report", help="Points left on the bench by week -- the Tier 2 headline metric"
    )
    bench_report_parser.add_argument("--league-id", default=LEAGUE_ID, help="League ID (default: configured league)")
    bench_report_parser.add_argument("--start-week", type=int, default=1, help="First week to include (default: 1)")
    bench_report_parser.add_argument(
        "--end-week", type=int, default=None, help="Last week to include (default: last completed week)"
    )
    bench_report_parser.set_defaults(func=cmd_bench_report)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
