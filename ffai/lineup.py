from ffai.models import LineupResult, LineupSlot
from ffai.vorp import DEDICATED_POSITIONS, FLEX_ELIGIBILITY

STARTING_SLOT_TYPES = DEDICATED_POSITIONS | set(FLEX_ELIGIBILITY)


def _eligible_positions(slot):
    return {slot} if slot in DEDICATED_POSITIONS else FLEX_ELIGIBILITY[slot]


def optimize_lineup(players, roster_positions):
    """Exact optimal starting lineup: maximizes total points subject to the
    league's slot eligibility (dedicated positions plus FLEX-type slots,
    sharing eligibility with vorp.FLEX_ELIGIBILITY). BN/IR/TAXI and any other
    non-starting slot names are ignored; every player not placed in a
    starting slot is bench by definition.

    Deliberately NOT vorp.compute_replacement_levels' "narrowest flex type
    first" heuristic -- that's a fine approximation for a replacement-level
    cutoff, but not always optimal when flex categories overlap without
    nesting (e.g. WRRB_FLEX + REC_FLEX + FLEX together). This needs to be
    exact: PRD's "points left on the bench" headline metric requires the
    optimal lineup computed exactly, no judgment involved, and Phase 3 reuses
    this same function unmodified for that (fed actual stats instead of
    projections).

    Implementation: this is a maximum-weight transversal-matroid basis.
    Process players by points descending, and place each one via an
    augmenting-path search (Kuhn's algorithm) that may bump an
    already-placed player to a different slot it's also eligible for to
    make room. Greedy-by-weight + augmenting-path membership test is the
    standard optimal algorithm for this exact structure."""
    slots = [pos for pos in roster_positions if pos in STARTING_SLOT_TYPES]
    assignment = {}  # slot index -> PlayerProjection

    def try_assign(player, visited_slots):
        for idx, slot in enumerate(slots):
            if idx in visited_slots or player.position not in _eligible_positions(slot):
                continue
            visited_slots.add(idx)
            occupant = assignment.get(idx)
            if occupant is None or try_assign(occupant, visited_slots):
                assignment[idx] = player
                return True
        return False

    ordered = sorted(players, key=lambda p: p.points, reverse=True)
    started_ids = set()
    for player in ordered:
        if try_assign(player, set()):
            started_ids.add(player.player_id)

    lineup_slots = [LineupSlot(slot=slot, player=assignment.get(idx)) for idx, slot in enumerate(slots)]
    bench = [p for p in ordered if p.player_id not in started_ids]
    total_points = round(sum(s.player.points for s in lineup_slots if s.player is not None), 2)
    return LineupResult(slots=lineup_slots, bench=bench, total_points=total_points)


def format_lineup(result):
    lines = [f"Optimal lineup ({result.total_points:.1f} pts):"]
    for slot in result.slots:
        if slot.player is None:
            lines.append(f"  {slot.slot}: (empty)")
        else:
            p = slot.player
            lines.append(f"  {slot.slot}: {p.name} ({p.position}, {p.team}) -- {p.points:.1f} pts")
    if result.bench:
        lines.append("Bench:")
        for p in result.bench:
            lines.append(f"  {p.name} ({p.position}, {p.team}) -- {p.points:.1f} pts")
    return "\n".join(lines)
