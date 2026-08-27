import statistics
from collections import Counter

from ffai.lineup import optimize_lineup
from ffai.models import WaiverTarget
from ffai.player_pool import build_projection, is_draftable
from ffai.projections import DEFAULT_POSITIONS
from ffai.vorp import DEDICATED_POSITIONS

# Sleeper's official API docs do not enumerate waiver_type's values. This
# mapping was confirmed empirically (2026-08-11) against the sandbox
# league's own League Settings > Waiver Type dropdown: the option order
# (Rolling, Reverse Standings, FAAB Bidding) matched the field's observed
# value of 0 for a league independently known to be set to "Rolling
# Waivers" via that same UI. Any value outside this map is treated as
# unknown and degrades to the priority-queue output path rather than
# guessing (PRD's "confidently wrong" anti-goal).
WAIVER_TYPE_LABELS = {
    0: "Rolling Waivers",
    1: "Reverse Standings",
    2: "FAAB Bidding",
}
FAAB_WAIVER_TYPE = 2

# FAAB bid-range heuristic: (low_pct, high_pct) of remaining budget per
# gap-clustered value tier (tier 1 = biggest cluster of marginal value,
# see faab_bid_ranges). Deliberately a documented heuristic, not a computed
# truth -- true market value depends on the whole league's behavior, which
# this tool has no visibility into. Tiers beyond the list reuse the last band.
FAAB_BID_BANDS = [(0.20, 0.35), (0.08, 0.20), (0.02, 0.08)]

# The bands above are RELATIVE -- they rank this week's targets against each
# other. On a quiet week that is not enough on its own: whoever happens to be
# best still lands in the top band, so replaying real 2025 weeks produced
# "bid $20-$35" (a third of a season's budget) for a player worth 1.8 points.
# Correct ordering, no sense of magnitude -- the same failure the draft side
# hit when it bought noise-sized edges with real picks.
#
# So a target also has to be worth something in absolute terms to reach the
# top bands. Thresholds are weekly marginal lineup points, matching the units
# WaiverTarget.marginal_value is already in, and deliberately reuse the
# judgment already documented in this file: priority_judgment has said "a few
# points" (3.0) is the bar for spending a waiver priority since it was
# written. A FAAB budget deserves the same bar.
WAIVER_WORTH_POINTS = 3.0
# Below this a "gain" is inside the week-to-week error of any projection and
# should not cost real money at all.
WAIVER_NOISE_POINTS = 1.0

# PRD 6.3: flag both over-conservatism (unspent budget at season end is
# pure waste) and early overspending. These deltas between fraction-of-
# budget-spent and fraction-of-season-elapsed are a rough pace check, not a
# precise model.
OVERSPENDING_DELTA = 0.25
CONSERVATIVE_DELTA = 0.35


def waiver_system_label(league_settings):
    return WAIVER_TYPE_LABELS.get(league_settings.get("waiver_type"), "Unknown waiver system (defaulting to priority-queue view)")


def is_faab(league_settings):
    return league_settings.get("waiver_type") == FAAB_WAIVER_TYPE


def compute_free_agents(players_raw, rosters, projections, scoring_settings, byes_by_team=None, positions=DEFAULT_POSITIONS):
    """Every draftable player not currently on any roster in the league --
    the waiver-wire pool. Shares is_draftable/build_projection with the
    draft board (player_pool.py) so the two stay consistent."""
    rostered_ids = {pid for roster in rosters for pid in (roster.get("players") or [])}
    free_agents = []
    for player_id, player in players_raw.items():
        if player_id in rostered_ids or not is_draftable(player, positions):
            continue
        projection = build_projection(player_id, players_raw, projections, scoring_settings, byes_by_team)
        if projection is not None:
            free_agents.append(projection)
    return free_agents


def _positions_needing_depth(my_players, roster_positions):
    """Dedicated positions where you don't have enough rostered players to
    even fill your starting slots -- the roster-need signal PRD 6.3 asks
    for when projections are degraded and point-based marginal value can't
    be computed. Deliberately ignores FLEX slots (ambiguous which position
    they'd draw from without points to compare) -- ok since this is only
    the last-resort ranking signal."""
    required = Counter(pos for pos in roster_positions if pos in DEDICATED_POSITIONS)
    have = Counter(p.position for p in my_players)
    return {pos for pos, count in required.items() if have.get(pos, 0) < count}


def rank_waiver_targets(free_agents, my_players, roster_positions, trending_counts=None, projections_degraded=False, limit=15):
    """Ranks free agents by the exact marginal points they'd add to your OWN
    optimal lineup right now (reuses lineup.optimize_lineup, the same exact
    optimizer the weekly brief uses) -- this single number already captures
    both raw value and roster need: a stud at a position you're already
    stacked at nets near-zero marginal value, while a modest player who
    fills a genuinely empty/weak slot scores meaningfully higher.

    Falls back to roster need (positions you can't even fill) plus
    trending-add velocity when projections are fully degraded, since
    marginal value is meaningless (all zeros) in that case -- PRD 6.3."""
    trending_counts = trending_counts or {}
    needy_positions = _positions_needing_depth(my_players, roster_positions) if projections_degraded else set()
    baseline = 0.0 if projections_degraded else optimize_lineup(my_players, roster_positions).total_points

    targets = []
    for player in free_agents:
        reasons = []
        trending = trending_counts.get(player.player_id)

        if projections_degraded:
            marginal_value = 0.0
            if player.position in needy_positions:
                reasons.append(f"roster need: you don't have enough {player.position}s to fill your starting lineup")
            if trending:
                reasons.append(f"trending: added in {trending} leagues recently")
            if not reasons:
                reasons.append("NO PROJECTIONS AVAILABLE -- ranked by trending-add velocity only")
        else:
            with_player = optimize_lineup(my_players + [player], roster_positions).total_points
            marginal_value = round(with_player - baseline, 2)
            if marginal_value > 0:
                reasons.append(f"would add {marginal_value:.1f} pts to your optimal lineup this week")
            else:
                reasons.append("would not crack your current starting lineup")
            if trending:
                reasons.append(f"trending: added in {trending} leagues recently")

        targets.append(WaiverTarget(player=player, marginal_value=marginal_value, trending_count=trending, reasons=reasons))

    if projections_degraded:
        targets.sort(key=lambda t: (t.player.position in needy_positions, t.trending_count or 0), reverse=True)
    else:
        targets.sort(key=lambda t: (t.marginal_value, t.trending_count or 0), reverse=True)

    return targets[:limit]


def faab_bid_ranges(targets, remaining_budget, gap_multiplier=2.0):
    """Tiers waiver targets by marginal_value using the same gap-based 1D
    clustering as vorp.build_tiers, then assigns each tier a bid-range
    heuristic (FAAB_BID_BANDS) as a percentage of remaining budget. Targets
    with zero/negative marginal value (wouldn't start on your team) get no
    bid recommendation. Returns {player_id: (low_dollars, high_dollars)}."""
    positive = [t for t in targets if t.marginal_value > 0]
    if not positive:
        return {}

    ordered = sorted(positive, key=lambda t: t.marginal_value, reverse=True)
    gaps = [ordered[i].marginal_value - ordered[i + 1].marginal_value for i in range(len(ordered) - 1)]
    threshold = statistics.median(gaps) * gap_multiplier if gaps else 0.0

    tiers = [[ordered[0]]]
    for i in range(1, len(ordered)):
        gap = ordered[i - 1].marginal_value - ordered[i].marginal_value
        if threshold > 0 and gap > threshold:
            tiers.append([])
        tiers[-1].append(ordered[i])

    result = {}
    for tier_index, tier_targets in enumerate(tiers):
        for t in tier_targets:
            # Relative tier sets the ceiling; absolute value decides whether
            # this target can actually reach it. A quiet week has no top-band
            # target in it, however the week's own targets rank against each
            # other.
            if t.marginal_value < WAIVER_NOISE_POINTS:
                continue  # not worth real money -- no bid at all
            band_index = tier_index if t.marginal_value >= WAIVER_WORTH_POINTS else len(FAAB_BID_BANDS) - 1
            low_pct, high_pct = FAAB_BID_BANDS[min(band_index, len(FAAB_BID_BANDS) - 1)]
            result[t.player.player_id] = (round(remaining_budget * low_pct), round(remaining_budget * high_pct))
    return result


def budget_pace_warning(league_settings, roster_settings, current_week, playoff_week_start=None):
    """PRD 6.3: flag both unspent-budget-at-season-end waste and early
    overspending, by comparing fraction of budget spent to fraction of the
    regular season elapsed. Returns None when there's not enough
    information to judge (no budget configured, no week known yet)."""
    total = league_settings.get("waiver_budget")
    if not total or not current_week:
        return None

    last_week = (playoff_week_start or league_settings.get("playoff_week_start") or 15) - 1
    if last_week <= 1:
        return None

    spent = (roster_settings or {}).get("waiver_budget_used", 0)
    frac_spent = spent / total
    frac_season = min(max((current_week - 1) / last_week, 0.0), 1.0)

    if frac_spent - frac_season > OVERSPENDING_DELTA:
        return (
            f"you've spent {frac_spent:.0%} of your FAAB budget but the season is only "
            f"{frac_season:.0%} through -- pace looks aggressive."
        )
    if frac_season - frac_spent > CONSERVATIVE_DELTA and frac_season > 0.3:
        return (
            f"you've only spent {frac_spent:.0%} of your FAAB budget with the season "
            f"{frac_season:.0%} through -- unspent budget at season end is pure waste."
        )
    return None


def priority_judgment(top_target, waiver_position=None, worth_threshold=3.0):
    """Simple judgment call for priority-queue (rolling/reverse-standings)
    systems: is the best available target worth spending your current
    priority position, or should you hold it for a better one? Threshold is
    a documented heuristic (PRD leaves this to judgment, not a formula) --
    a few points of projected marginal lineup value."""
    if top_target is None or top_target.marginal_value < worth_threshold:
        note = "nothing on the wire looks worth spending your priority on this week -- hold it."
    else:
        note = f"{top_target.player.name} would add {top_target.marginal_value:.1f} pts -- worth using your priority."
    if waiver_position:
        note = f"Your waiver priority: #{waiver_position}. {note}"
    return note


def format_waiver_report(targets, waiver_label, faab, remaining_budget=None, faab_bids=None, pace_warning=None, priority_note=None):
    lines = [f"Waiver targets ({waiver_label}):"]
    if not targets:
        lines.append("  No free agents found.")
        return "\n".join(lines)

    faab_bids = faab_bids or {}
    for target in targets:
        p = target.player
        line = f"  {p.name} ({p.position}, {p.team})"
        if faab and p.player_id in faab_bids:
            low, high = faab_bids[p.player_id]
            line += f" -- bid ${low}-${high}"
        if target.reasons:
            line += f" -- {'; '.join(target.reasons)}"
        lines.append(line)

    if faab:
        if remaining_budget is not None:
            lines.append(f"Remaining budget: ${remaining_budget}")
        if pace_warning:
            lines.append(f"BUDGET: {pace_warning}")
    elif priority_note:
        lines.append(priority_note)

    return "\n".join(lines)
