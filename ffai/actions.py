from dataclasses import dataclass

from ffai.lineup import eligible_positions

# A starting slot producing less than this is not really filled. Deliberately
# above zero: a healthy starter projected 2 pts is just as much a hole as an
# empty slot, and the Week 3 2026 brief that prompted this module showed a
# 0.0-pt QB in the lineup with no comment at all. Set at the same magnitude
# as waivers.WAIVER_WORTH_POINTS -- below that, a swap isn't worth a move.
HOLE_POINT_THRESHOLD = 5.0

# Statuses that mean the player will not play (or is a coin flip to). These
# produce a hole REGARDLESS of the projection, because a stale projection for
# a ruled-out player is exactly the "confidently wrong" case the PRD warns
# about -- the number can still read 18.0 the morning after the news breaks.
NOT_PLAYING_STATUSES = {"Out", "IR", "PUP", "Suspended", "Doubtful"}


@dataclass
class LineupHole:
    slot: str
    player: "PlayerProjection | None"
    reason: str  # human-readable, already explains itself -- PRD 6.2 "reasoning, not bare numbers"
    points: float
    severity: str  # "critical" (will not play / empty) | "weak" (playing, but projected low)


@dataclass
class ActionItem:
    hole: LineupHole
    fix: "WaiverTarget | None"  # best waiver-wire replacement eligible for this slot
    bid: "tuple | None"  # (low, high) dollars, when the league uses FAAB
    gain: float  # projected points the fix adds over what's in the slot now


def find_lineup_holes(lineup_result, players_raw, week=None):
    """Starting slots that aren't really producing: empty, filled by someone
    who won't play, on bye, or projected below HOLE_POINT_THRESHOLD.

    This is the check the brief was missing. The Week 3 2026 brief rendered
    `QB: Jayden Daniels -- 0.0 pts [Out]` and said nothing else: the data was
    all there and the conclusion was never drawn."""
    holes = []
    for slot in lineup_result.slots:
        player = slot.player

        if player is None:
            holes.append(LineupHole(
                slot=slot.slot, player=None,
                reason=f"no player in your {slot.slot} slot at all",
                points=0.0, severity="critical",
            ))
            continue

        status = (players_raw.get(player.player_id) or {}).get("injury_status")
        if status in NOT_PLAYING_STATUSES:
            holes.append(LineupHole(
                slot=slot.slot, player=player,
                reason=f"{player.name} is listed **{status}**",
                points=player.points, severity="critical",
            ))
            continue

        if week is not None and player.bye_week == week:
            holes.append(LineupHole(
                slot=slot.slot, player=player,
                reason=f"{player.name} is on bye this week",
                points=player.points, severity="critical",
            ))
            continue

        if player.points < HOLE_POINT_THRESHOLD:
            holes.append(LineupHole(
                slot=slot.slot, player=player,
                reason=f"{player.name} is projected just {player.points:.1f} pts",
                points=player.points, severity="weak",
            ))

    return holes


def recommend_fixes(holes, targets, bids=None, min_gain=1.0):
    """Pairs each lineup hole with the best waiver target eligible for that
    slot. Reuses lineup.eligible_positions so FLEX holes correctly consider
    every flex-eligible position rather than just one.

    A target is only offered when it beats what's already in the slot by
    `min_gain` -- below that the swap is inside projection noise and costs a
    roster move for nothing (the same judgment waivers.py applies to money)."""
    bids = bids or {}
    claimed = set()  # one target can only fill one hole -- don't recommend the same QB twice
    items = []

    # Critical holes pick their replacement first: a slot with nobody playing
    # has a stronger claim on the best available player than a merely weak one.
    for hole in sorted(holes, key=lambda h: h.severity != "critical"):
        allowed = eligible_positions(hole.slot)
        best = None
        for target in targets:  # already ranked best-first by waivers.rank_waiver_targets
            if target.player.player_id in claimed or target.player.position not in allowed:
                continue
            best = target
            break

        gain = 0.0
        if best is not None:
            gain = round(best.player.points - hole.points, 2)
            if gain < min_gain:
                best = None

        if best is not None:
            claimed.add(best.player.player_id)

        items.append(ActionItem(
            hole=hole,
            fix=best,
            bid=bids.get(best.player.player_id) if best else None,
            gain=gain if best else 0.0,
        ))

    return items
