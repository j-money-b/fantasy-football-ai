from datetime import datetime, timezone

from ffai.brief_model import (
    SWAP_NOISE_POINTS,
    build_brief_model,
    depth_note,
    margin_sentence,
    pickup_instruction,
    status_label,
    todo,
)

# Block-drawing characters, not emoji (explicit user preference) and not
# images: GitHub renders issue comments as markdown with no CSS, so a bar
# built from text is the only chart that survives the trip into an email
# notification. Wrapped in backticks at render time so the monospace font
# keeps every bar the same physical width.
BAR_FULL = "█"
BAR_EMPTY = "░"
BAR_WIDTH = 12


def render_brief_markdown(context, waiver_plan=None, now=None):
    """Full weekly brief. `context` is a weekly_context.WeeklyContext and
    `waiver_plan` an optional waivers.WaiverPlan -- this function only
    renders; every decision lives in brief_model."""
    model = build_brief_model(context, waiver_plan, now=now)
    lines = [f"# Week {context.week} Brief", ""]
    lines.append(f"`Last successful run: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}`")

    if context.warnings:  # R1: unmissable, accurate staleness/degradation banner
        lines += ["", "> [!WARNING]", "> **DATA MAY BE STALE OR INCOMPLETE -- DO NOT FULLY TRUST THIS BRIEF**", ">"]
        lines += [f"> - {warning}" for warning in context.warnings]

    if not model.has_roster:
        return "\n".join(lines + ["", "No roster found for you in this league yet."])

    if model.degraded:
        lines += ["", "**NO PROJECTIONS AVAILABLE** -- showing your currently-set Sleeper lineup only, not optimized.", ""]
        lines += [f"- {p.name} ({p.position}, {p.team})" for p in model.current_starters] or ["*(no lineup set yet)*"]
        return "\n".join(lines)

    lines += _todo_section(model)
    lines += _section("Set your lineup", _lineup_section(context, model))
    lines += _section("Pickups", _moves_section(model))
    lines += _section("On the wire right now", _wire_section(model))
    lines += _section(f"Matchup -- Week {context.week}", _opponent_section(context, model))
    return "\n".join(lines)


def _section(title, body):
    return ["", "---", "", f"## {title}"] + body


# --- to-do -----------------------------------------------------------------

def _todo_section(model):
    items = todo(model)
    if not items:
        return ["", "> [!NOTE]", "> **Nothing to do this week.** Your best lineup is set and nothing on the wire beats your roster."]
    lines = ["", "> [!IMPORTANT]", "> **Do this:**", ">"]
    lines += [f"> {index}. {item}" for index, item in enumerate(items, start=1)]
    if model.moves and model.window:
        lines += [">", f"> *{model.window.text}*"]
    return lines


# --- lineup ----------------------------------------------------------------

def _lineup_section(context, model):
    lines = [""]
    if model.changes:
        lines.append("**Changes to make in Sleeper**")
        lines.append("")
        for change in model.changes:
            if change.start and change.sit:
                lines.append(f"- **Start {change.start.name}** ({change.start.position}), **bench {change.sit.name}** -- {change.reason}")
            elif change.start:
                lines.append(f"- **Start {change.start.name}** -- {change.reason}")
            else:
                lines.append(f"- **Bench {change.sit.name}** -- {change.reason}")
        if 0 < model.swap_gain < SWAP_NOISE_POINTS:
            lines += ["", f"*Worth only +{model.swap_gain:.1f} pts -- inside projection noise. "
                          f"Make it if you're already in the app, skip it if you're not.*"]
        lines.append("")
    else:
        lines += ["Your lineup in Sleeper is already the best one you can set.", ""]

    if model.dead_slots:
        fixing = {m.add.position for m in model.moves if m.gain_this_week > 0}
        slots = ", ".join(model.dead_slots)
        note = " -- see Pickups below for the fix." if fixing else " -- nobody on your roster or the wire fills it."
        lines += [f"> [!CAUTION]", f"> **No one on your roster can score at {slots} this week**{note}", ""]

    if model.locked:
        names = ", ".join(p.name for p in model.locked)
        lines += [f"*Already played this week and locked in place: {names}.*", ""]

    result = model.lineup
    scale = max([s.player.points for s in result.slots if s.player] + [1.0])
    lines.append(f"**Best lineup: {result.total_points:.1f} pts**")
    lines.append("")
    lines.append("| Slot | Player | Proj | |")
    lines.append("|:--|:--|--:|:--|")
    for slot in result.slots:
        if slot.player is None:
            lines.append(f"| **{slot.slot}** | *(empty)* | -- | `{BAR_EMPTY * BAR_WIDTH}` |")
            continue
        lines.append(f"| **{slot.slot}** | {_player_cell(slot.player, context.players_raw)} "
                     f"| **{slot.player.points:.1f}** | `{_bar(slot.player.points, scale)}` |")

    if result.bench:
        lines += ["", "**Bench**", "", "| Player | Proj | |", "|:--|--:|:--|"]
        for player in result.bench:
            lines.append(f"| {_player_cell(player, context.players_raw)} | {player.points:.1f} | `{_bar(player.points, scale)}` |")
    return lines


# --- pickups ---------------------------------------------------------------

def _moves_section(model):
    plan = model.plan
    lines = [""]
    if plan is not None and plan.faab and plan.remaining_budget is not None:
        lines += [f"**FAAB budget left: ${plan.remaining_budget}** -- FAAB is your season's waiver money; "
                  f"each week the highest blind bid on a player wins him.", ""]

    if not model.moves:
        lines.append(f"No add/drop is worth making right now: nobody on the wire would improve your lineup by "
                     f"more than a point a week over weeks {_span(model)}. The full scan is below.")
        return lines + _pace(plan)

    for index, move in enumerate(model.moves, start=1):
        add, drop = move.add, move.drop
        lines.append(f"### {index}. Add {add.name} ({add.position}, {add.team}) -- drop {drop.name} ({drop.position})")
        lines.append("")
        lines.append("| | |")
        lines.append("|:--|:--|")
        lines.append(f"| Do it | {pickup_instruction(model, move)} |")
        if move.worth is not None and move.worth > 1:
            lines.append(f"| Most he's worth to you | ${move.worth} |")
        if move.market:
            lines.append(f"| Market | {move.market}"
                         f"{f' ({move.trending_count:,} adds in the last day)' if move.trending_count else ''} |")
        lines.append(f"| Gain | **+{move.gain:.1f} pts** over weeks {_span(model)} (+{move.gain_this_week:.1f} this week) |")
        lines.append(f"| Points by week | {_by_week(move.weeks, move.add_by_week)} vs. {drop.name}: {_by_week(move.weeks, move.drop_by_week)} |")
        lines.append("")
        lines.append("**Why:**")
        lines += [f"- {reason}" for reason in move.reasons]
        lines.append("")

    if len(model.moves) > 1:
        lines.append("*Each move assumes you made the ones above it.*")
    return lines + _pace(plan)


def _pace(plan):
    if plan is None or not plan.pace_warning:
        return []
    return ["", "> [!TIP]", f"> **Budget pace:** {plan.pace_warning}"]


# --- wire scan -------------------------------------------------------------

def _wire_section(model):
    lines = ["", f"The best available at each position, over weeks {_span(model)}. "
                 "**Gain** is what he'd add to your team after cutting your least useful player for him -- "
                 "zero or less means he wouldn't help you."]
    if not model.wire:
        return lines + ["", "No free agents found."]

    lines += ["", "| Pos | Player | Pts (4 wk) | Gain | Would replace | Adds today |", "|:--|:--|--:|--:|:--|--:|"]
    for position, entries in model.wire.items():
        for entry in entries:
            lines.append(_wire_row(position, entry))

    if model.rising:
        lines += ["", "**Who everyone else is adding** -- usually a role change or an injury ahead of him on the depth chart:", ""]
        lines += ["| Pos | Player | Pts (4 wk) | Gain | Adds today |", "|:--|:--|--:|--:|--:|"]
        for entry in model.rising:
            p = entry.player
            lines.append(f"| {p.position} | {_name(p)} | {entry.horizon_points:.1f} | {_gain(entry.gain)} | {entry.trending_count:,} |")
    return lines


def _wire_row(position, entry):
    p = entry.player
    drop = entry.best_drop.name if entry.best_drop else "--"
    adds = f"{entry.trending_count:,}" if entry.trending_count else "--"
    return f"| {position} | {_name(p)} | {entry.horizon_points:.1f} | {_gain(entry.gain)} | {drop} | {adds} |"


def _name(player):
    flag = f" **[{player.unavailable}]**" if player.unavailable else ""
    return f"{player.name} `{player.team}`{flag}"


def _gain(gain):
    return f"**+{gain:.1f}**" if gain >= 1 else f"{gain:+.1f}"


# --- matchup ---------------------------------------------------------------

def _opponent_section(context, model):
    if context.opponent_roster is None:
        return ["", "No matchup data available yet."]
    lines = ["", f"Facing **{context.opponent_display_name}**."]
    mine, theirs = model.my_total, model.opponent_total or 0.0
    scale = max(mine, theirs, 1.0)
    lines += ["", "| | Projected | |", "|:--|--:|:--|"]
    lines.append(f"| **You** (best lineup) | **{mine:.1f}** | `{_bar(mine, scale)}` |")
    lines.append(f"| **{context.opponent_display_name}** (as set) | **{theirs:.1f}** | `{_bar(theirs, scale)}` |")
    lines += ["", margin_sentence(model) or ""]

    lines += ["", f"**{context.opponent_display_name}'s currently-set starters**", "", "| Player | Proj |", "|:--|--:|"]
    for player in model.opponent_starters:
        lines.append(f"| {player.name} ({player.position}, {player.team}){_flag(player, context.players_raw)} | {player.points:.1f} |")
    if not model.opponent_starters:
        lines.append("| *(no lineup set yet)* | -- |")
    return lines


# --- shared helpers --------------------------------------------------------

def _span(model):
    weeks = model.horizon_weeks or [model.week]
    return f"{weeks[0]}-{weeks[-1]}" if len(weeks) > 1 else str(weeks[0])


def _by_week(weeks, points):
    return ", ".join(f"wk{w} {p:.1f}" for w, p in zip(weeks, points))


def _bar(points, scale, width=BAR_WIDTH):
    filled = int(round(max(points, 0.0) / scale * width)) if scale > 0 else 0
    filled = min(filled, width)
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def _flag(player, players_raw):
    label = status_label(player, players_raw)
    return f" **[{label}]**" if label else ""


def _player_cell(player, players_raw):
    depth = depth_note(player.player_id, players_raw)
    return f"{player.name} `{player.team}`{_flag(player, players_raw)}{f' `depth: {depth}`' if depth else ''}"
