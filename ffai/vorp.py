import statistics
from collections import Counter, defaultdict

from ffai.models import PlayerVorp, Tier

DEDICATED_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}

FLEX_ELIGIBILITY = {
    "FLEX": {"RB", "WR", "TE"},
    "WRRB_FLEX": {"WR", "RB"},
    "REC_FLEX": {"WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
}


def compute_replacement_levels(ranked_players, roster_positions, total_rosters):
    """Replacement level per position: the points value of the best player who
    would NOT be a starter anywhere, given the league's actual roster
    construction. Dedicated slots (QB/RB/WR/TE/K/DEF) claim their exact
    count x total_rosters. FLEX-type slots are filled greedily, one at a
    time, from the pool of players not already claimed by a dedicated slot --
    narrower-eligibility flex types (e.g. WRRB_FLEX) go first since they're
    more constrained than broad ones (e.g. SUPER_FLEX). Slot names not in
    DEDICATED_POSITIONS or FLEX_ELIGIBILITY (e.g. BN, IR, TAXI) are ignored."""
    by_position = defaultdict(list)
    for player in ranked_players:
        by_position[player.position].append(player)
    for players in by_position.values():
        players.sort(key=lambda p: p.points, reverse=True)

    dedicated_counts = Counter(pos for pos in roster_positions if pos in DEDICATED_POSITIONS)
    dedicated_demand = {pos: total_rosters * count for pos, count in dedicated_counts.items()}

    flex_counts = Counter(pos for pos in roster_positions if pos in FLEX_ELIGIBILITY)

    flex_eligible_positions = set()
    for eligible in FLEX_ELIGIBILITY.values():
        flex_eligible_positions |= eligible

    remainder = []
    for pos in flex_eligible_positions:
        players = by_position.get(pos, [])
        cutoff = dedicated_demand.get(pos, 0)
        remainder.extend(players[cutoff:])
    remainder.sort(key=lambda p: p.points, reverse=True)

    flex_demand = Counter()
    for flex_type, count in sorted(flex_counts.items(), key=lambda kv: len(FLEX_ELIGIBILITY[kv[0]])):
        slots_to_fill = total_rosters * count
        eligible = FLEX_ELIGIBILITY[flex_type]
        filled = 0
        still_remaining = []
        for player in remainder:
            if filled < slots_to_fill and player.position in eligible:
                flex_demand[player.position] += 1
                filled += 1
            else:
                still_remaining.append(player)
        remainder = still_remaining

    total_demand = dict(dedicated_demand)
    for pos, count in flex_demand.items():
        total_demand[pos] = total_demand.get(pos, 0) + count

    replacement_levels = {}
    for pos, players in by_position.items():
        demand = total_demand.get(pos, 0)
        if not players:
            replacement_levels[pos] = 0.0
        elif demand < len(players):
            replacement_levels[pos] = players[demand].points
        else:
            replacement_levels[pos] = players[-1].points  # demand exceeds supply -- worst available is the floor
    return replacement_levels


def compute_vorp(ranked_players, replacement_levels):
    """VORP = points above replacement level at the player's own position.
    Sorted descending -- this is the cross-position "best player available"
    ordering the draft assistant's big board is built from."""
    result = [
        PlayerVorp(
            player_id=p.player_id,
            name=p.name,
            position=p.position,
            team=p.team,
            bye_week=p.bye_week,
            points=p.points,
            data_source=p.data_source,
            vorp=round(p.points - replacement_levels.get(p.position, 0.0), 2),
        )
        for p in ranked_players
    ]
    result.sort(key=lambda p: p.vorp, reverse=True)
    return result


def build_tiers(players, gap_multiplier=2.0):
    """Gap-based 1D clustering on VORP, computed SEPARATELY within each
    position -- VORP scale differs wildly by position (RB/WR spread over
    100+ points; DEF/K over single digits), so a single global gap
    threshold would be set by whichever position has the biggest gaps
    (RB/WR) and would swallow DEF/K's own real cliffs into one
    catch-all bottom tier, and would blur genuine same-position cliffs
    that in-draft scarcity decisions depend on (DraftAssistant.
    _recommend_healthy's cliff-urgency bonus needs same-position,
    same-tier comparisons to mean something). Mutates each player's .tier
    in place -- numbering restarts at 1 within each position -- and
    returns all tier groupings across every position (order across
    positions not guaranteed)."""
    if not players:
        return []

    by_position = defaultdict(list)
    for p in players:
        by_position[p.position].append(p)

    all_tiers = []
    for position_players in by_position.values():
        ordered = sorted(position_players, key=lambda p: p.vorp, reverse=True)
        gaps = [ordered[i].vorp - ordered[i + 1].vorp for i in range(len(ordered) - 1)]
        threshold = statistics.median(gaps) * gap_multiplier if gaps else 0.0

        current = [ordered[0]]
        tier_number = 1
        for i in range(1, len(ordered)):
            gap = ordered[i - 1].vorp - ordered[i].vorp
            if threshold > 0 and gap > threshold:
                all_tiers.append(_finalize_tier(tier_number, current))
                tier_number += 1
                current = []
            current.append(ordered[i])
        all_tiers.append(_finalize_tier(tier_number, current))

    return all_tiers


def _finalize_tier(tier_number, players):
    for p in players:
        p.tier = tier_number
    vorps = [p.vorp for p in players]
    return Tier(tier_number=tier_number, players=players, min_vorp=min(vorps), max_vorp=max(vorps))
