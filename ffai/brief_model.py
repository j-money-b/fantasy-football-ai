"""The brief's content, decided once, independent of how it's rendered.

There are two renderers (markdown for the GitHub thread, HTML for the
emailed version) and they must never disagree about what this week's
recommendation is. Everything either of them needs to DECIDE lives here;
the renderers only choose how to say it.

The brief answers three questions, in this order:
  1. Is my lineup right? (who to start/sit, and why)
  2. Who should I pick up, who do I cut for him, and what do I bid?
  3. What else is on the wire? -- every time, whether or not anything's wrong.
The old version only looked at the wire when a starter had a problem, and
told the user to "hold" ruled-out starters because their projections were
never zeroed (see availability.py)."""

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from ffai.availability import AT_RISK, OUT_THIS_WEEK, describe_status
from ffai.lineup import STARTING_SLOT_TYPES, optimize_lineup
from ffai.models import LineupResult, LineupSlot

INJURY_STATUSES_WORTH_FLAGGING = OUT_THIS_WEEK | AT_RISK

# A lineup swap worth less than this is inside projection noise: worth doing
# if you're already in the Sleeper app, not worth a notification.
SWAP_NOISE_POINTS = 1.0

EASTERN = ZoneInfo("America/New_York")
# Observed from this league's own transactions (2026-10-27): waiver claims
# all complete Wednesday ~07:00 UTC, and free-agent adds pile up right after.
# So Sun-Tue is bidding season; Wed-Sat unclaimed players are free agents.
WAIVER_RUN_WEEKDAY = 2  # Monday=0
WAIVER_RUN_TEXT = "Wednesday ~3am ET"


@dataclass
class LineupChange:
    start: "PlayerProjection | None"
    sit: "PlayerProjection | None"
    reason: str


@dataclass
class WaiverWindow:
    mode: str  # "bid" (claims pending) | "add" (free agency open)
    text: str  # one line on how to make a pickup right now


@dataclass
class BriefModel:
    week: "int | None"
    warnings: list
    has_roster: bool
    degraded: bool
    window: "WaiverWindow | None" = None

    lineup: "LineupResult | None" = None
    changes: list = field(default_factory=list)  # list[LineupChange]
    swap_gain: float = 0.0
    dead_slots: list = field(default_factory=list)  # slots nobody on the roster can fill this week
    locked: list = field(default_factory=list)  # players whose game is over -- can't be moved

    opponent_name: "str | None" = None
    opponent_starters: list = field(default_factory=list)
    my_total: float = 0.0
    opponent_total: "float | None" = None
    margin: "float | None" = None

    moves: list = field(default_factory=list)  # list[roster_moves.RosterMove]
    wire: dict = field(default_factory=dict)  # position -> list[roster_moves.WireEntry]
    rising: list = field(default_factory=list)  # list[roster_moves.WireEntry]
    horizon_weeks: list = field(default_factory=list)
    plan: "WaiverPlan | None" = None

    current_starters: list = field(default_factory=list)  # degraded-path fallback view


def build_brief_model(context, waiver_plan=None, now=None):
    model = BriefModel(
        week=context.week,
        warnings=list(context.warnings),
        has_roster=context.my_roster is not None,
        degraded=context.projections_degraded,
        opponent_name=context.opponent_display_name,
        plan=waiver_plan,
        window=waiver_window(now),
    )

    if context.my_roster is None:
        return model

    if context.projections_degraded:
        model.current_starters = _starters(context.my_roster, context.my_players)
        return model

    model.lineup, model.locked = recommend_lineup(
        context.my_players, context.roster_positions, context.my_roster.get("starters") or [], context.already_played
    )
    model.my_total = model.lineup.total_points
    model.dead_slots = [s.slot for s in model.lineup.slots if s.player is None or s.player.points <= 0]
    model.changes, model.swap_gain = lineup_changes(context, model.lineup)

    if context.opponent_roster is not None:
        model.opponent_starters = _starters(context.opponent_roster, context.opponent_players)
        model.opponent_total = round(sum(p.points for p in model.opponent_starters), 2)
        model.margin = round(model.my_total - model.opponent_total, 1)

    if waiver_plan is not None and waiver_plan.moves is not None:
        model.moves = waiver_plan.moves.moves
        model.wire = waiver_plan.moves.wire
        model.rising = waiver_plan.moves.rising
        model.horizon_weeks = waiver_plan.moves.weeks

    return model


def waiver_window(now=None):
    now = (now or datetime.now(EASTERN)).astimezone(EASTERN)
    # Sunday(6), Monday(0), Tuesday(1): players are locked to waivers until
    # the Wednesday run. Wednesday through Saturday: free agency.
    if now.weekday() in (6, 0, 1):
        return WaiverWindow(
            mode="bid",
            text=f"Waivers run {WAIVER_RUN_TEXT}. Place these as FAAB bids in Sleeper before then -- "
                 f"highest bid wins, and you only pay if you win.",
        )
    return WaiverWindow(
        mode="add",
        text="Waivers already ran this week, so unclaimed players are free agents: tap Add in Sleeper, "
             "no bid needed, first come first served. (If Sleeper shows a waiver clock on him instead, "
             "he was just dropped -- bid the amount shown.)",
    )


def recommend_lineup(players, roster_positions, current_starters, already_played):
    """Optimal lineup, respecting games already played: a starter whose game
    is over is stuck in his slot, and a benched one can't be moved in.
    Sleeper's `starters` list is ordered like the starting slots in
    roster_positions, so position i of one is slot i of the other."""
    slots = [pos for pos in roster_positions if pos in STARTING_SLOT_TYPES]
    locked_slots = {}
    for index, player_id in enumerate(current_starters[: len(slots)]):
        if player_id in already_played:
            locked_slots[index] = player_id

    by_id = {p.player_id: p for p in players}
    open_indexes = [i for i in range(len(slots)) if i not in locked_slots]
    movable = [p for p in players if p.player_id not in already_played]
    partial = optimize_lineup(movable, [slots[i] for i in open_indexes])

    filled = {}
    for index, player_id in locked_slots.items():
        filled[index] = by_id.get(player_id)
    for index, slot in zip(open_indexes, partial.slots):
        filled[index] = slot.player

    lineup_slots = [LineupSlot(slot=slot, player=filled.get(i)) for i, slot in enumerate(slots)]
    started = {s.player.player_id for s in lineup_slots if s.player is not None}
    bench = sorted((p for p in players if p.player_id not in started), key=lambda p: p.points, reverse=True)
    total = round(sum(s.player.points for s in lineup_slots if s.player is not None), 2)
    locked = [by_id[pid] for pid in locked_slots.values() if pid in by_id]
    return LineupResult(slots=lineup_slots, bench=bench, total_points=total), locked


def lineup_changes(context, lineup):
    """Start/sit pairs vs. what's set in Sleeper right now, each with the
    reason. Paired same-position first so "start Godwin instead of Egbuka"
    reads as one decision rather than two unrelated lists."""
    current_ids = [pid for pid in context.my_roster.get("starters") or [] if pid != "0"]
    recommended_ids = {s.player.player_id for s in lineup.slots if s.player is not None}
    by_id = {p.player_id: p for p in context.my_players}

    starts = [by_id[pid] for pid in sorted(recommended_ids - set(current_ids)) if pid in by_id]
    sits = [by_id[pid] for pid in current_ids if pid not in recommended_ids and pid in by_id]
    if not starts and not sits:
        return [], 0.0
    gain = round(sum(p.points for p in starts) - sum(p.points for p in sits), 2)

    changes = []
    remaining = list(starts)
    for sit in sorted(sits, key=lambda p: p.points):
        partner = next((s for s in remaining if s.position == sit.position), None) or (remaining[0] if remaining else None)
        if partner is not None:
            remaining.remove(partner)
        changes.append(LineupChange(start=partner, sit=sit, reason=_change_reason(partner, sit)))
    for start in remaining:  # an empty slot being filled -- nobody to sit
        changes.append(LineupChange(start=start, sit=None, reason=f"your lineup has an empty slot -- {start.name} fills it"))
    return changes, gain


def _change_reason(start, sit):
    if sit.unavailable:
        why = f"{sit.name} is {sit.unavailable} and won't score"
        return f"{why} -- {start.name} projects {start.points:.1f}." if start else f"{why}."
    if start is None:
        return f"{sit.name} shouldn't be starting."
    return f"{start.name} projects {start.points:.1f} vs. {sit.points:.1f} for {sit.name}."


def margin_sentence(model):
    if model.margin is None:
        return None
    if model.margin >= 0:
        return f"You're projected to **win by {model.margin:.1f}**."
    return f"You're projected to **lose by {abs(model.margin):.1f}**."


def todo(model):
    """The numbered to-do list at the top of the brief: every action, in the
    order to do them, each one line. Plain text with markdown emphasis; the
    HTML renderer strips it."""
    if not model.has_roster or model.degraded:
        return []
    items = []
    for change in model.changes:
        if change.start and change.sit:
            items.append(f"**Start {change.start.name}** instead of {change.sit.name} -- {change.reason}")
        elif change.start:
            items.append(f"**Start {change.start.name}** -- {change.reason}")
        else:
            items.append(f"**Bench {change.sit.name}** -- {change.reason}")
    for move in model.moves:
        items.append(f"**Add {move.add.name}, drop {move.drop.name}** -- {pickup_instruction(model, move)}.")
    return items


def pickup_instruction(model, move):
    if move.bid is None:
        return "claim him on waivers"
    low, high = move.bid
    amount = f"${low}" if low == high else f"${low}-${high}"
    if model.window is not None and model.window.mode == "add":
        return f"add now as a free agent (if he's on waivers, bid {amount})"
    return f"bid {amount} before {WAIVER_RUN_TEXT}"


def headline(model):
    """One sentence for the email preheader and the top of the brief."""
    if not model.has_roster:
        return "No roster found for you in this league yet."
    if model.degraded:
        return "Projections are unavailable -- showing your currently-set lineup only, not optimized."
    parts = []
    if model.changes:
        parts.append(f"{len(model.changes)} lineup {'change' if len(model.changes) == 1 else 'changes'}")
    if model.moves:
        parts.append(f"{len(model.moves)} {'pickup' if len(model.moves) == 1 else 'pickups'} worth making")
    if not parts:
        return "Your lineup is set and nothing on the wire beats your roster. Nothing to do."
    first = todo(model)[0].replace("**", "")
    return f"{' and '.join(parts).capitalize()}. First: {first}"


def injury_status(player_id, players_raw):
    status = (players_raw.get(player_id) or {}).get("injury_status")
    return status if status in INJURY_STATUSES_WORTH_FLAGGING else None


def status_label(player, players_raw):
    """What to show beside a name: the reason he won't play, or a risk flag."""
    if player.unavailable:
        return player.unavailable
    status = injury_status(player.player_id, players_raw)
    return describe_status(player.player_id, players_raw) if status else None


def depth_note(player_id, players_raw):
    player = players_raw.get(player_id) or {}
    position = player.get("depth_chart_position")
    order = player.get("depth_chart_order")
    return f"{position}{order}" if position and order and order > 1 else None


def _starters(roster, players):
    by_id = {p.player_id: p for p in players}
    return [by_id[pid] for pid in (roster.get("starters") or []) if pid != "0" and pid in by_id]
