"""Backtest draft strategies against real Sleeper mock drafts.

Our slot picks by the strategy under test; the other nine teams pick by
one of two opponent models. Final rosters are scored by optimal starting-
lineup points, so strategies are compared on outcome rather than on
whether individual picks "look" reasonable.

Two opponent models matter because the mock drafts this was validated
against were played by Sleeper autopick bots, which take the highest
projected points available and so hammer RB far harder than humans do.
Tuning against that alone would overfit to bot behaviour, so "needs"
models a human-ish table that stops loading up on a position once it has
enough of them and leaves K/DEF until late.
"""
import json
import sys
from collections import Counter

from ffai.byes import load_bye_weeks
from ffai.cache import Cache
from ffai.config import LEAGUE_ID
from ffai.draft_assistant import DraftAssistant
from ffai.lineup import optimize_lineup
from ffai.player_pool import is_draftable, to_projection_pool
from ffai.projections import ProjectionsAdapter
from ffai.repository import fetch_league, fetch_players, fetch_projections
from ffai.sleeper_client import SleeperClient
from ffai.vorp import build_tiers, compute_replacement_levels, compute_vorp

# What a typical human manager will carry at each position before they
# stop taking more, and how late they leave the streamable slots.
HUMAN_POSITION_CAPS = {"QB": 2, "RB": 5, "WR": 5, "TE": 2, "K": 1, "DEF": 1}
HUMAN_LATE_ROUND_POSITIONS = {"K": 9, "DEF": 8}


def load_board():
    client = SleeperClient()
    cache = Cache()
    adapter = ProjectionsAdapter()
    league, _, _ = fetch_league(client, cache, LEAGUE_ID)
    players_raw, _, _ = fetch_players(client, cache)
    scoring = league.get("scoring_settings", {})
    roster_positions = league.get("roster_positions", [])
    total_rosters = league.get("total_rosters")
    season = league.get("season")
    known = {pid for pid, p in players_raw.items() if is_draftable(p)}
    projections, _, _ = fetch_projections(adapter, cache, players_raw, season, known_player_ids=known)
    pool = to_projection_pool(players_raw, projections, scoring, byes_by_team=load_bye_weeks())
    levels = compute_replacement_levels(pool, roster_positions, total_rosters)
    board = compute_vorp(pool, levels)
    build_tiers(board)
    consensus = adapter.fetch_consensus_top10(players_raw)
    return client, cache, board, roster_positions, total_rosters, consensus


def slot_for_pick(pick_no, total_rosters):
    round_no = (pick_no - 1) // total_rosters + 1
    pos = (pick_no - 1) % total_rosters + 1
    return pos if round_no % 2 == 1 else total_rosters - pos + 1


def simulate(strategy, adp_ids, board, roster_positions, total_rosters, consensus,
             client, cache, my_slot, rounds, opponents="adp"):
    adp_rank = {pid: i for i, pid in enumerate(adp_ids)}
    vorp_rank = {p.player_id: i for i, p in enumerate(sorted(board, key=lambda p: p.vorp, reverse=True))}

    def adp_key(p):
        return adp_rank.get(p.player_id, 10_000 + vorp_rank[p.player_id])

    assistant = DraftAssistant(
        client, cache, "sim", board, roster_positions, my_slot,
        board_degraded=False, consensus_top10=consensus, total_rosters=total_rosters,
    )

    drafted = set()
    my_roster = []
    my_picks = []
    picks = []
    opponent_rosters = {slot: Counter() for slot in range(1, total_rosters + 1)}

    def pick_for_opponent(slot, round_no):
        best = None
        best_key = None
        for p in board:
            if p.player_id in drafted:
                continue
            if opponents == "needs":
                if opponent_rosters[slot][p.position] >= HUMAN_POSITION_CAPS.get(p.position, 99):
                    continue
                if round_no < HUMAN_LATE_ROUND_POSITIONS.get(p.position, 0):
                    continue
            key = adp_key(p)
            if best_key is None or key < best_key:
                best, best_key = p, key
        return best

    for pick_no in range(1, rounds * total_rosters + 1):
        slot = slot_for_pick(pick_no, total_rosters)
        round_no = (pick_no - 1) // total_rosters + 1

        if slot == my_slot:
            if strategy == "adp":
                choice = pick_for_opponent(slot, round_no)
            else:
                assistant.drafted_player_ids = drafted
                assistant.my_drafted_players = my_roster
                assistant.picks = picks
                rec = assistant.recommend()
                choice = rec.player if rec else None
            if choice is None:
                break
            my_roster.append(choice)
            my_picks.append((pick_no, choice))
        else:
            choice = pick_for_opponent(slot, round_no)
            if choice is None:
                break

        drafted.add(choice.player_id)
        opponent_rosters[slot][choice.position] += 1
        picks.append({"pick_no": pick_no, "player_id": choice.player_id, "draft_slot": slot})

    return my_roster, optimize_lineup(my_roster, roster_positions), my_picks


# Share of a season a starter at each position is expected to miss. Kept
# HERE rather than imported from draft_assistant so the strategy is not
# graded by the same constants it optimises against -- if these two ever
# need to disagree, this is the one that decides who won.
EVAL_MISS_RATE = {"RB": 0.20, "WR": 0.15, "TE": 0.15, "QB": 0.12, "K": 0.03, "DEF": 0.0}


def score_roster(roster, roster_positions):
    """Returns (healthy, expected).

    `healthy` is optimal starting-lineup points with nobody hurt -- the
    measure this harness used to report on its own, and the reason it
    could not see the bug it was built to catch. Bench players contribute
    exactly 0 to it, so a roster that spends round 8 on a kicker and
    rounds 11-14 on tight ends it can never start scores identically to
    one that used those picks on real depth. Optimising against it alone
    rewards hoarding whoever has the highest raw projection on the bench.

    `expected` charges each starter's expected missed games against what
    the lineup falls to without them, so cover is worth something and
    redundant cover is not. Judge changes on this one; `healthy` is kept
    alongside it because a change that raises expected while lowering
    healthy is usually right, and you want to see both halves of that
    trade rather than be surprised by it."""
    lineup = optimize_lineup(roster, roster_positions)
    healthy = lineup.total_points
    penalty = 0.0
    for slot in lineup.slots:
        starter = slot.player
        if starter is None:
            continue
        miss_rate = EVAL_MISS_RATE.get(starter.position, 0.0)
        if not miss_rate:
            continue
        without = [p for p in roster if p.player_id != starter.player_id]
        penalty += miss_rate * (healthy - optimize_lineup(without, roster_positions).total_points)
    return healthy, healthy - penalty


def main():
    picks_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/picks3.json"
    real_picks = sorted(json.load(open(picks_path)), key=lambda p: p["pick_no"])
    adp_ids = [p["player_id"] for p in real_picks]

    client, cache, board, roster_positions, total_rosters, consensus = load_board()
    rounds = len(roster_positions)

    print(f"{total_rosters} teams, {rounds} rounds, roster {roster_positions}\n")

    for opponents in ("adp", "needs"):
        print(f"=== opponent model: {opponents} ===")
        results = {}
        for strategy in ("tool", "adp"):
            healthy_totals, expected_totals, kdef_rounds = [], [], []
            for my_slot in range(1, total_rosters + 1):
                roster, _lineup, my_picks = simulate(
                    strategy, adp_ids, board, roster_positions, total_rosters, consensus,
                    client, cache, my_slot, rounds, opponents=opponents,
                )
                healthy, expected = score_roster(roster, roster_positions)
                healthy_totals.append(healthy)
                expected_totals.append(expected)
                kdef_rounds.extend(
                    i + 1 for i, (_pick_no, p) in enumerate(my_picks) if p.position in ("K", "DEF")
                )
            results[strategy] = expected_totals
            avg_kdef = sum(kdef_rounds) / len(kdef_rounds) if kdef_rounds else float("nan")
            print(f"  {strategy:5s} expected {sum(expected_totals)/len(expected_totals):7.1f}   "
                  f"healthy {sum(healthy_totals)/len(healthy_totals):7.1f}   "
                  f"avg K/DEF round {avg_kdef:4.1f}")
        wins = sum(1 for t, a in zip(results["tool"], results["adp"]) if t > a)
        edge = (sum(results["tool"]) - sum(results["adp"])) / total_rosters
        print(f"  tool beats ADP baseline in {wins}/{total_rosters} draft slots, "
              f"avg edge {edge:+.1f} expected pts\n")


if __name__ == "__main__":
    main()
