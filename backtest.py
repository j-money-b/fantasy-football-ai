"""Backtest draft strategies against a table of human managers.

Our slot picks by the strategy under test; the other nine teams pick by
one of two opponent models. Final rosters are scored by optimal starting-
lineup points, so strategies are compared on outcome rather than on
whether individual picks "look" reasonable.

The pick order underneath both opponent models now defaults to real human
ADP (ffai/adp.py) at this league's exact shape. It used to be seeded from
a single completed Sleeper mock, which was a real problem: those mocks
were played by autopick bots that take the highest projected total
available, so they clear the RB board far earlier than humans do. Every
scarcity number the recommender had ever been graded on was measured
against a table running the one strategy it exists to beat, on a board
that strategy had itself distorted. Pass a picks JSON as argv[1] to
replay a specific draft instead.

Three opponent models, because ADP fixes what order players go in, not
how a manager reacts to their own roster or to the table. "adp" follows
the order blindly; "needs" stops loading up on a position once it has
enough and leaves K/DEF until late; "runs" adds positional herding.

"runs" exists because the first two are both ADP-SHAPED, and that blind
spot hid a real bug. The recommender used to forecast the rest of the
draft by extrapolating this draft's own observed positional mix, which is
accurate exactly when the table follows ADP -- so the harness graded the
forecast against the one opponent distribution it could not get wrong,
and reported the tool beating every baseline in 10/10 slots while a live
Sleeper mock had it take four running backs and no wide receiver. Under
"runs" the table chases whatever position is already going, the observed
mix diverges from ADP, and the failure reproduces. Any future change to
the forecast should be judged on this model, not just the friendly two.
"""
import json
import sys
from collections import Counter

from ffai.adp import fetch_adp
from ffai.byes import load_bye_weeks
from ffai.cache import Cache
from ffai.config import LEAGUE_ID
from ffai.draft_assistant import DraftAssistant
from ffai.lineup import optimize_lineup
from ffai.models import PlayerProjection
from ffai.player_pool import is_draftable, to_projection_pool
from ffai.projections import ProjectionsAdapter
from ffai.repository import fetch_league, fetch_players, fetch_projections
from ffai.sleeper_client import SleeperClient
from ffai.vorp import build_tiers, compute_replacement_levels, compute_vorp

# What a typical human manager will carry at each position before they
# stop taking more, and how late they leave the streamable slots.
HUMAN_POSITION_CAPS = {"QB": 2, "RB": 5, "WR": 5, "TE": 2, "K": 1, "DEF": 1}
HUMAN_LATE_ROUND_POSITIONS = {"K": 9, "DEF": 8}

# Positional-run herding, for the "runs" opponent model. Window and
# threshold match DraftAssistant.detect_positional_run so the table the
# harness builds is running the behaviour the tool claims to detect.
# RUN_ADP_PULL is how many ADP slots a manager will reach ahead to chase a
# run: 12 is roughly a round in a 10-team league, enough to bend the
# observed positional mix well away from ADP without making the table
# absurd.
RUN_WINDOW = 5
RUN_THRESHOLD = 3
RUN_ADP_PULL = 12


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
    return client, cache, board, roster_positions, total_rosters, consensus, players_raw, season


def open_slot_positions(roster, roster_positions):
    """Positions that would land in an empty starting slot right now --
    i.e. what a manager drafting "by need" would still be shopping for.
    Probes with a 1-point dummy at each position and asks whether the
    optimal lineup gains a filled slot, so FLEX eligibility is handled by
    optimize_lineup rather than re-derived here."""
    base = sum(1 for s in optimize_lineup(roster, roster_positions).slots if s.player is not None)
    open_positions = set()
    for position in {"QB", "RB", "WR", "TE", "K", "DEF"}:
        dummy = PlayerProjection(
            player_id="__probe__", name="probe", position=position,
            team=None, bye_week=None, points=1.0, data_source="none",
        )
        filled = sum(
            1 for s in optimize_lineup(roster + [dummy], roster_positions).slots if s.player is not None
        )
        if filled > base:
            open_positions.add(position)
    return open_positions


def slot_for_pick(pick_no, total_rosters):
    round_no = (pick_no - 1) // total_rosters + 1
    pos = (pick_no - 1) % total_rosters + 1
    return pos if round_no % 2 == 1 else total_rosters - pos + 1


def simulate(strategy, adp_ids, board, roster_positions, total_rosters, consensus,
             client, cache, my_slot, rounds, opponents="adp", forecast_adp_ids=None):
    """`adp_ids` orders the TABLE. `forecast_adp_ids` is what the tool is
    allowed to believe about that order, and they are deliberately not the
    same argument.

    In replay mode `adp_ids` is the pick order of a draft that already
    happened. Handing that to the recommender as its ADP would give it
    perfect foreknowledge of every pick the table is about to make, which is
    not a backtest -- it grades the tool on a draft it can see the end of.
    Defaults to `adp_ids` for the ordinary case, where the order genuinely IS
    public human ADP and there is nothing to leak."""
    adp_rank = {pid: i for i, pid in enumerate(adp_ids)}
    vorp_rank = {p.player_id: i for i, p in enumerate(sorted(board, key=lambda p: p.vorp, reverse=True))}
    board_by_id = {p.player_id: p for p in board}

    def adp_key(p):
        return adp_rank.get(p.player_id, 10_000 + vorp_rank[p.player_id])

    assistant = DraftAssistant(
        client, cache, "sim", board, roster_positions, my_slot,
        board_degraded=False, consensus_top10=consensus, total_rosters=total_rosters,
        adp_ranks={
            player_id: rank
            for rank, player_id in enumerate(forecast_adp_ids or adp_ids, start=1)
        },
    )

    drafted = set()
    my_roster = []
    my_picks = []
    picks = []
    opponent_rosters = {slot: Counter() for slot in range(1, total_rosters + 1)}

    def running_position():
        """The position the table is currently chasing, if any -- same
        window/threshold the recommender uses to call a run out loud."""
        recent = [
            board_by_id[pk["player_id"]].position
            for pk in picks[-RUN_WINDOW:]
            if pk["player_id"] in board_by_id
        ]
        if not recent:
            return None
        position, count = Counter(recent).most_common(1)[0]
        return position if count >= RUN_THRESHOLD else None

    def pick_for_opponent(slot, round_no):
        chasing = running_position() if opponents == "runs" else None
        best = None
        best_key = None
        for p in board:
            if p.player_id in drafted:
                continue
            if opponents in ("needs", "runs"):
                if opponent_rosters[slot][p.position] >= HUMAN_POSITION_CAPS.get(p.position, 99):
                    continue
                if round_no < HUMAN_LATE_ROUND_POSITIONS.get(p.position, 0):
                    continue
            key = adp_key(p)
            # Herding: a manager watching a position empty out reaches for
            # it ahead of ADP. Deterministic rather than sampled so runs are
            # reproducible between harness invocations.
            if chasing is not None and p.position == chasing:
                key -= RUN_ADP_PULL
            if best_key is None or key < best_key:
                best, best_key = p, key
        return best

    for pick_no in range(1, rounds * total_rosters + 1):
        slot = slot_for_pick(pick_no, total_rosters)
        round_no = (pick_no - 1) // total_rosters + 1

        if slot == my_slot:
            if strategy == "adp":
                choice = pick_for_opponent(slot, round_no)
            elif strategy == "points+need":
                # The fair version of "just grab the highest projection":
                # highest projected total among positions that still have an
                # empty starting slot, falling back to the whole board once
                # the lineup is full. Still blind to scarcity -- it never
                # asks who will still be there at the next turn.
                open_positions = open_slot_positions(my_roster, roster_positions)
                pool = [
                    p for p in board
                    if p.player_id not in drafted and (not open_positions or p.position in open_positions)
                ]
                choice = max(pool, key=lambda p: p.points, default=None)
            elif strategy == "points":
                # "Just take the highest projected total left" -- no notion of
                # positional scarcity, roster slots or replacement level. The
                # naive baseline the tool has to beat to justify existing.
                choice = max(
                    (p for p in board if p.player_id not in drafted),
                    key=lambda p: p.points,
                    default=None,
                )
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
    picks_path = sys.argv[1] if len(sys.argv) > 1 else None

    client, cache, board, roster_positions, total_rosters, consensus, players_raw, season = load_board()
    rounds = len(roster_positions)

    # Default to real human ADP. A single completed draft can be passed
    # instead, but note what that costs: every draft available to replay so
    # far was Sleeper autopick bots, whose pick order is a projection column
    # read top-down. Grading positional-scarcity logic against that measures
    # the tool against the strategy it exists to beat, on a board shaped by
    # that same strategy. ADP is 7k+ real human drafts at this league's exact
    # shape, which is the table the tool will actually face.
    # What the tool is allowed to believe about the table is ALWAYS public
    # human ADP, whichever order the table itself is running -- see simulate().
    forecast_adp_ids, stale, meta = fetch_adp(cache, players_raw, teams=total_rosters, season=season)
    adp_label = (
        f"human ADP ({meta.get('total_drafts')} {meta.get('type')} drafts, "
        f"{meta.get('teams')}-team, {meta.get('start_date')}..{meta.get('end_date')})"
        + (" [STALE CACHE]" if stale else "")
    )

    if picks_path:
        real_picks = sorted(json.load(open(picks_path)), key=lambda p: p["pick_no"])
        adp_ids = [p["player_id"] for p in real_picks]
        order_label = f"single draft replay ({picks_path}, {len(adp_ids)} picks)"
    else:
        adp_ids = forecast_adp_ids
        order_label = adp_label

    print(f"{total_rosters} teams, {rounds} rounds, roster {roster_positions}")
    print(f"pick order: {order_label}")
    print(f"tool's forecast reads: {adp_label}\n")

    for opponents in ("adp", "needs", "runs"):
        print(f"=== opponent model: {opponents} ===")
        results = {}
        for strategy in ("tool", "adp", "points+need", "points"):
            healthy_totals, expected_totals, kdef_rounds = [], [], []
            for my_slot in range(1, total_rosters + 1):
                roster, _lineup, my_picks = simulate(
                    strategy, adp_ids, board, roster_positions, total_rosters, consensus,
                    client, cache, my_slot, rounds, opponents=opponents,
                    forecast_adp_ids=forecast_adp_ids,
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
        for baseline, label in (("adp", "ADP"), ("points+need", "projected-points+need"), ("points", "raw projected-points")):
            wins = sum(1 for t, b in zip(results["tool"], results[baseline]) if t > b)
            edge = (sum(results["tool"]) - sum(results[baseline])) / total_rosters
            print(f"  tool beats {label} baseline in {wins}/{total_rosters} draft slots, "
                  f"avg edge {edge:+.1f} expected pts")
        print()


if __name__ == "__main__":
    main()
