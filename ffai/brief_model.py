"""The brief's content, decided once, independent of how it's rendered.

There are two renderers (markdown for the GitHub thread, HTML for the
emailed version) and they must never disagree about what this week's
recommendation is. Everything either of them needs to DECIDE lives here;
the renderers only choose how to say it."""

from dataclasses import dataclass, field

from ffai.actions import find_lineup_holes, recommend_fixes
from ffai.lineup import optimize_lineup
from ffai.waivers import WAIVER_NOISE_POINTS

INJURY_STATUSES_WORTH_FLAGGING = {"Questionable", "Doubtful", "Out", "IR", "PUP", "Suspended"}

# A lineup swap worth less than this is inside projection noise: worth doing
# if you're already in the Sleeper app, not worth a notification. Matches the
# floor waivers.py applies before spending money.
SWAP_NOISE_POINTS = 1.0


@dataclass
class BriefModel:
    week: "int | None"
    warnings: list
    has_roster: bool
    degraded: bool

    lineup: "LineupResult | None" = None
    items: list = field(default_factory=list)  # list[actions.ActionItem]
    action_gain: float = 0.0

    starts: list = field(default_factory=list)  # list[PlayerProjection] to move into the lineup
    sits: list = field(default_factory=list)  # list[PlayerProjection] to move out
    swap_gain: float = 0.0

    opponent_name: "str | None" = None
    opponent_starters: list = field(default_factory=list)
    my_total: float = 0.0
    opponent_total: "float | None" = None
    margin: "float | None" = None

    upgrades: list = field(default_factory=list)  # waiver targets that help this week
    speculative: list = field(default_factory=list)  # no lineup value yet, but the market is buying
    plan: "WaiverPlan | None" = None

    current_starters: list = field(default_factory=list)  # degraded-path fallback view


def build_brief_model(context, waiver_plan=None):
    model = BriefModel(
        week=context.week,
        warnings=list(context.warnings),
        has_roster=context.my_roster is not None,
        degraded=context.projections_degraded,
        opponent_name=context.opponent_display_name,
        plan=waiver_plan,
    )

    if context.my_roster is None:
        return model

    if context.projections_degraded:
        # Every slot would look like a hole with no projections, so the action
        # list is suppressed entirely rather than filled with false alarms.
        model.current_starters = _starters(context.my_roster, context.my_players)
        return model

    model.lineup = optimize_lineup(context.my_players, context.roster_positions)
    model.my_total = model.lineup.total_points

    holes = find_lineup_holes(model.lineup, context.players_raw, context.week)
    model.items = recommend_fixes(
        holes,
        waiver_plan.ranked if waiver_plan else [],
        waiver_plan.bids if waiver_plan else {},
    )
    model.action_gain = round(sum(item.gain for item in model.items), 1)

    model.starts, model.sits, model.swap_gain = _lineup_changes(context, model.lineup)

    if context.opponent_roster is not None:
        model.opponent_starters = _starters(context.opponent_roster, context.opponent_players)
        model.opponent_total = round(sum(p.points for p in model.opponent_starters), 2)
        model.margin = round(model.my_total - model.opponent_total, 1)

    if waiver_plan is not None:
        model.upgrades = [t for t in waiver_plan.targets if t.marginal_value >= WAIVER_NOISE_POINTS]
        speculative = [
            t for t in waiver_plan.targets
            if t.marginal_value < WAIVER_NOISE_POINTS and t.trending_count
        ]
        speculative.sort(key=lambda t: t.trending_count, reverse=True)
        model.speculative = speculative[:5]

    return model


def upgrades_by_position(model):
    grouped = {}
    for target in model.upgrades:
        grouped.setdefault(target.player.position, []).append(target)
    return {position: grouped[position] for position in sorted(grouped)}


def margin_sentence(model):
    """One plain-English line on whether doing the work actually flips the
    week -- the number a manager decides on, rather than two totals to
    subtract for themselves."""
    if model.margin is None:
        return None
    # Emphasis is written in markdown here and stripped by the HTML renderer
    # (email_brief._plain), the same convention the hole reasons already use --
    # one phrasing decision, rendered two ways.
    if model.margin >= 0:
        return f"You're projected to **win by {model.margin:.1f}**."
    deficit = abs(model.margin)
    if model.action_gain <= 0:
        return f"You're projected to **lose by {deficit:.1f}**."
    if model.action_gain >= deficit:
        return (
            f"You're projected to **lose by {deficit:.1f}** -- but making every move above "
            f"is worth **+{model.action_gain:.1f}**, which flips this matchup."
        )
    return (
        f"You're projected to **lose by {deficit:.1f}**. Making every move above is worth "
        f"**+{model.action_gain:.1f}**, closing most of the gap but not all of it."
    )


def injury_status(player_id, players_raw):
    status = (players_raw.get(player_id) or {}).get("injury_status")
    return status if status in INJURY_STATUSES_WORTH_FLAGGING else None


def depth_note(player_id, players_raw):
    player = players_raw.get(player_id) or {}
    position = player.get("depth_chart_position")
    order = player.get("depth_chart_order")
    return f"{position}{order}" if position and order and order > 1 else None


def _starters(roster, players):
    by_id = {p.player_id: p for p in players}
    return [by_id[pid] for pid in (roster.get("starters") or []) if pid != "0" and pid in by_id]


def _lineup_changes(context, lineup):
    current_ids = {pid for pid in context.my_roster.get("starters") or [] if pid != "0"}
    recommended_ids = {s.player.player_id for s in lineup.slots if s.player is not None}
    if not current_ids or current_ids == recommended_ids:
        return [], [], 0.0

    by_id = {p.player_id: p for p in context.my_players}
    starts = [by_id[pid] for pid in sorted(recommended_ids - current_ids) if pid in by_id]
    sits = [by_id[pid] for pid in sorted(current_ids - recommended_ids) if pid in by_id]
    gain = round(sum(p.points for p in starts) - sum(p.points for p in sits), 2)
    return starts, sits, gain
