from collections import Counter, defaultdict

from ffai.lineup import optimize_lineup
from ffai.models import TradeEvaluation, TradeSideResult
from ffai.vorp import DEDICATED_POSITIONS


def resolve_player_ids(names, players_raw):
    """Resolves each full player name to a Sleeper player_id via exact
    case-insensitive match against the player directory. Raises ValueError
    (the CLI turns this into a clean error message, not a stack trace) on
    zero or multiple matches -- PRD's "confidently wrong" anti-goal applies
    to silently guessing which player was meant just as much as a bad
    number."""
    resolved = []
    for name in names:
        matches = [pid for pid, p in players_raw.items() if (p.get("full_name") or "").lower() == name.lower()]
        if len(matches) == 0:
            raise ValueError(f"no player found matching {name!r}")
        if len(matches) > 1:
            raise ValueError(f"multiple players match {name!r} ({len(matches)} matches) -- ambiguous")
        resolved.append(matches[0])
    return resolved


def evaluate_trade(party_a, party_b, board_by_id, roster_positions):
    """Two-team trade evaluation (PRD 6.4). `board_by_id` is a season-long
    VORP board (player_id -> PlayerVorp, from vorp.py) covering every
    draftable player regardless of current roster status -- reuses the same
    value-over-replacement machinery the draft assistant uses, since VORP
    is exactly "value relative to the field," the right lens for judging a
    trade too, not just raw points.

    For each side, `value_sent`/`value_received` are summed season VORP of
    what left/arrived -- but `lineup_delta` is the more decisive number: the
    change in that team's optimal SEASON-AGGREGATE lineup total (the same
    exact optimizer lineup.py uses for weekly starts, fed season totals
    instead) before vs. after the swap. This is what actually answers "does
    this trade help you" -- a redundant addition at an already-strong
    position can carry big VORP but ~zero lineup_delta, and that
    VORP-vs-lineup_delta disagreement IS the "accounts for roster
    construction/positional need" reasoning PRD 6.4 asks for, not something
    bolted on separately.

    Deliberate simplification: lineup_delta treats the whole season as one
    static lineup-optimization problem, not a week-by-week simulation (that
    would need weekly rest-of-season projections per remaining week, which
    the projections adapter doesn't provide -- R5, PRD 4.2). Bye-week
    collisions are instead flagged separately as a qualitative warning."""
    side_a = _evaluate_side(party_a, party_b, board_by_id, roster_positions)
    side_b = _evaluate_side(party_b, party_a, board_by_id, roster_positions)
    return TradeEvaluation(sides=[side_a, side_b], verdict=_verdict(side_a, side_b))


def _evaluate_side(party, other_party, board_by_id, roster_positions):
    sends = [board_by_id[pid] for pid in party.sends_ids if pid in board_by_id]
    receives = [board_by_id[pid] for pid in other_party.sends_ids if pid in board_by_id]

    value_sent = round(sum(p.vorp for p in sends), 2)
    value_received = round(sum(p.vorp for p in receives), 2)

    current_roster = [board_by_id[pid] for pid in party.roster_player_ids if pid in board_by_id]
    sends_ids = set(party.sends_ids)
    roster_after = [p for p in current_roster if p.player_id not in sends_ids] + receives

    lineup_before = optimize_lineup(current_roster, roster_positions).total_points
    lineup_after = optimize_lineup(roster_after, roster_positions).total_points

    return TradeSideResult(
        label=party.label,
        sends=sends,
        receives=receives,
        value_sent=value_sent,
        value_received=value_received,
        net_vorp=round(value_received - value_sent, 2),
        lineup_delta=round(lineup_after - lineup_before, 2),
        bye_week_warnings=_bye_week_collisions(roster_after, roster_positions),
    )


def _bye_week_collisions(roster_after, roster_positions):
    """Flags dedicated positions with more than one starting slot where 2+
    rostered players (post-trade) share a bye week -- on that week you'd be
    down to fewer viable starters at that position than usual. A single-slot
    position sharing a bye is normal/unavoidable, not a trade-specific
    problem, so those are skipped."""
    multi_slot_positions = {
        pos for pos, count in Counter(p for p in roster_positions if p in DEDICATED_POSITIONS).items() if count > 1
    }

    by_position_and_bye = defaultdict(list)
    for p in roster_after:
        if p.position in multi_slot_positions and p.bye_week:
            by_position_and_bye[(p.position, p.bye_week)].append(p)

    warnings = []
    for (position, bye_week), players in by_position_and_bye.items():
        if len(players) > 1:
            names = ", ".join(p.name for p in players)
            warnings.append(f"{position}: {names} all have bye week {bye_week}")
    return warnings


def _verdict(side_a, side_b):
    if side_a.lineup_delta > 0 and side_b.lineup_delta > 0:
        return "Both teams' optimal lineups improve -- looks like a win-win (redundant depth moved to where it's useful)."
    if side_a.lineup_delta == side_b.lineup_delta:
        return "Roughly even -- neither side's optimal lineup gains more than the other."
    favors = side_a.label if side_a.lineup_delta > side_b.lineup_delta else side_b.label
    return (
        f"Favors {favors} by starting-lineup impact "
        f"({side_a.label}: {side_a.lineup_delta:+.1f} pts, {side_b.label}: {side_b.lineup_delta:+.1f} pts)."
    )


def format_trade_evaluation(evaluation):
    lines = ["Trade evaluation:"]
    for side in evaluation.sides:
        lines.append("")
        lines.append(f"{side.label}:")
        lines.append(f"  Sends: {', '.join(p.name for p in side.sends) or '(nothing)'}")
        lines.append(f"  Receives: {', '.join(p.name for p in side.receives) or '(nothing)'}")
        lines.append(
            f"  Season VORP: {side.value_sent:.1f} sent, {side.value_received:.1f} received "
            f"(net {side.net_vorp:+.1f})"
        )
        lines.append(f"  Optimal-lineup impact: {side.lineup_delta:+.1f} pts")
        for warning in side.bye_week_warnings:
            lines.append(f"  BYE WEEK WARNING: {warning}")

    lines.append("")
    lines.append(f"Verdict: {evaluation.verdict}")
    return "\n".join(lines)
