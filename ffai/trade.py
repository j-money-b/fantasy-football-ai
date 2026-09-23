from collections import Counter, defaultdict

from ffai.lineup import optimize_lineup
from ffai.models import TradeEvaluation, TradeSideResult
from ffai.vorp import DEDICATED_POSITIONS

# Statuses meaning the player cannot take the field right now. Sleeper uses
# "NA" for non-injury inactives (suspensions, personal leave) -- which is
# exactly how a suspended player appeared while this was being written, with
# a full healthy season projection still attached to him.
UNAVAILABLE_STATUSES = {"Out", "IR", "PUP", "Suspended", "NA", "DNR", "COV"}
# Week-to-week, usually plays. Worth surfacing next to a trade -- a knee with
# meniscus surgery behind it is not the same risk as a routine tag -- but not
# grounds for distrusting the numbers outright.
RISK_STATUSES = {"Questionable", "Doubtful"}

NFL_GAMES = 17


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


def evaluate_trade(party_a, party_b, board_by_id, roster_positions, players_raw=None):
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
    collisions are instead flagged separately as a qualitative warning.

    `players_raw` is the Sleeper player dictionary. Without it this function
    values every player at a healthy full-season projection and says nothing
    about whether he can play at all -- which priced a player on the inactive
    list at 139 season points and a QB with a dislocated elbow at 309. The
    projections never learn about availability, so the status field has to be
    read directly, the same way actions.py refuses to trust a projection
    sitting beside an "Out" tag."""
    players_raw = players_raw or {}
    side_a = _evaluate_side(party_a, party_b, board_by_id, roster_positions, players_raw)
    side_b = _evaluate_side(party_b, party_a, board_by_id, roster_positions, players_raw)
    trustworthy = not any(
        _is_unavailable(p, players_raw) for side in (side_a, side_b) for p in side.sends + side.receives
    )
    return TradeEvaluation(
        sides=[side_a, side_b],
        verdict=_verdict(side_a, side_b, trustworthy),
        projections_trustworthy=trustworthy,
    )


def _evaluate_side(party, other_party, board_by_id, roster_positions, players_raw=None):
    sends = [board_by_id[pid] for pid in party.sends_ids if pid in board_by_id]
    receives = [board_by_id[pid] for pid in other_party.sends_ids if pid in board_by_id]

    value_sent = round(sum(p.vorp for p in sends), 2)
    value_received = round(sum(p.vorp for p in receives), 2)

    current_roster = [board_by_id[pid] for pid in party.roster_player_ids if pid in board_by_id]
    sends_ids = set(party.sends_ids)
    roster_after = [p for p in current_roster if p.player_id not in sends_ids] + receives

    lineup_before = optimize_lineup(current_roster, roster_positions).total_points
    after = optimize_lineup(roster_after, roster_positions)
    lineup_after = after.total_points

    return TradeSideResult(
        label=party.label,
        sends=sends,
        receives=receives,
        value_sent=value_sent,
        value_received=value_received,
        net_vorp=round(value_received - value_sent, 2),
        lineup_delta=round(lineup_after - lineup_before, 2),
        bye_week_warnings=_bye_week_collisions(roster_after, roster_positions),
        availability_warnings=(
            _availability_warnings(sends, receives, players_raw or {})
            + _unavailable_in_lineup(after, players_raw or {})
        ),
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


def _status_of(player, players_raw):
    return (players_raw.get(player.player_id) or {}).get("injury_status")


def _is_unavailable(player, players_raw):
    return _status_of(player, players_raw) in UNAVAILABLE_STATUSES


def _detail(player, players_raw):
    info = players_raw.get(player.player_id) or {}
    bits = [b for b in (info.get("injury_body_part"), info.get("injury_notes")) if b]
    return f" ({' - '.join(bits)})" if bits else ""


def _availability_warnings(sends, receives, players_raw):
    """Flags players in the deal who cannot currently play, and says what
    their season projection is actually assuming.

    Deliberately states the per-game arithmetic instead of inventing a
    discount: nothing here knows a return date, and guessing one would be
    exactly the confidently-wrong number the PRD forbids. Points-per-game is
    arithmetic the reader can apply to their own estimate of games missed."""
    warnings = []
    for player, direction in [(p, "You'd receive") for p in receives] + [(p, "You'd send") for p in sends]:
        status = _status_of(player, players_raw)
        if status in UNAVAILABLE_STATUSES:
            per_game = player.points / NFL_GAMES if player.points else 0.0
            warnings.append(
                f"{direction} {player.name}, who is listed {status}{_detail(player, players_raw)} "
                f"and CANNOT PLAY right now. His {player.points:.0f}-pt season projection assumes he plays "
                f"every game -- roughly {per_game:.1f} pts of it per game missed."
            )
        elif status in RISK_STATUSES:
            warnings.append(
                f"{direction} {player.name}, listed {status}{_detail(player, players_raw)} -- "
                f"usually still plays, but the projection does not price the risk."
            )
    return warnings


def _unavailable_in_lineup(lineup, players_raw):
    """Flags players who cannot currently play but are still filling a slot in
    the post-trade optimal lineup, inflating that side's total.

    The trade above may be between two perfectly healthy players and still be
    judged against a lineup total that assumes a suspended running back and a
    quarterback with a dislocated elbow both suit up. Flagging the players
    swapped is not enough -- the roster they are swapped INTO is what the
    lineup_delta is computed over."""
    warnings = []
    for slot in lineup.slots:
        player = slot.player
        if player is not None and _is_unavailable(player, players_raw):
            warnings.append(
                f"Their optimal lineup still counts {player.name} at {slot.slot} "
                f"({player.points:.0f} pts), who is listed {_status_of(player, players_raw)}"
                f"{_detail(player, players_raw)} and cannot play -- the lineup total above "
                f"is inflated by that much."
            )
    return warnings


def _verdict(side_a, side_b, trustworthy=True):
    if not trustworthy:
        return (
            "CANNOT BE JUDGED ON PROJECTIONS ALONE -- a player in this deal cannot currently play, "
            "and the season projections above assume he plays every game. Decide how many games you "
            "expect him to miss before trusting any number here."
        )
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
        for warning in side.availability_warnings:
            lines.append(f"  AVAILABILITY: {warning}")
        for warning in side.bye_week_warnings:
            lines.append(f"  BYE WEEK WARNING: {warning}")

    lines.append("")
    lines.append(f"Verdict: {evaluation.verdict}")
    return "\n".join(lines)
