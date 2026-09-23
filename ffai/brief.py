from datetime import datetime, timezone

from ffai.brief_model import (
    INJURY_STATUSES_WORTH_FLAGGING,
    SWAP_NOISE_POINTS,
    build_brief_model,
    margin_sentence,
    upgrades_by_position,
)

# Block-drawing characters, not emoji (explicit user preference) and not
# images: GitHub renders issue comments as markdown with no CSS, so a bar
# built from text is the only chart that survives the trip into an email
# notification. Wrapped in backticks at render time so the monospace font
# keeps every bar the same physical width.
BAR_FULL = "█"
BAR_EMPTY = "░"
BAR_WIDTH = 12


def render_brief_markdown(context, waiver_plan=None):
    """Full weekly brief (PRD 6.2/6.3 + reliability requirements R1/R2/R6).
    `context` is a weekly_context.WeeklyContext and `waiver_plan` an optional
    waivers.WaiverPlan -- this function only renders; all data gathering and
    computation already happened upstream."""
    lines = [f"# Week {context.week} Brief", ""]

    lines.append(f"`Last successful run: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}`")

    if context.warnings:  # R1: unmissable, accurate staleness/degradation banner
        lines.append("")
        lines.append("> [!WARNING]")
        lines.append("> **DATA MAY BE STALE OR INCOMPLETE -- DO NOT FULLY TRUST THIS BRIEF**")
        lines.append(">")
        for warning in context.warnings:
            lines.append(f"> - {warning}")

    # Content decisions live in brief_model, shared with the HTML renderer,
    # so the emailed brief and the GitHub thread can never disagree about
    # what this week's recommendation is.
    model = build_brief_model(context, waiver_plan)

    lines.extend(_render_action_section(model))

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Your Lineup")
    lines.extend(_render_my_lineup_section(context, model))

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"## Matchup -- Week {context.week}")
    lines.extend(_render_opponent_section(context, model))

    if waiver_plan is not None:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## Waiver Wire")
        lines.extend(_render_waiver_section(model))

    return "\n".join(lines)


# --- action items ----------------------------------------------------------

def _render_action_section(model):
    """The section the brief was missing. Everything here was already
    computable before -- a Week 3 2026 brief showed a 0.0-pt ruled-out QB in
    the starting lineup and drew no conclusion from it."""
    waiver_plan = model.plan
    items = model.items
    if model.lineup is None:
        return []
    if not items:
        return ["", "> [!NOTE]", "> **No lineup holes.** Every starting slot is filled by someone playing this week."]

    critical = sum(1 for item in items if item.hole.severity == "critical")
    lines = ["", "> [!CAUTION]"]
    headline = f"**{len(items)} lineup {'hole' if len(items) == 1 else 'holes'} to fix before kickoff"
    lines.append(f"> {headline}{f' -- {critical} critical' if critical else ''}.**")
    lines.append(">")
    for item in items:
        lines.append(f"> - **{item.hole.slot}** -- {item.hole.reason}")

    lines.append("")
    lines.append("## Action Required")

    for index, item in enumerate(items, start=1):
        lines.append("")
        lines.append(f"### {index}. {item.hole.slot} -- {item.hole.reason}")
        lines.append("")
        lines.append("| | |")
        lines.append("|:--|:--|")

        if item.hole.player is None:
            lines.append(f"| In your lineup | *nothing -- the {item.hole.slot} slot is empty* |")
        else:
            lines.append(f"| In your lineup | {item.hole.player.name} `{item.hole.player.team}` -- **{item.hole.points:.1f}** pts |")

        if item.fix is None:
            lines.append("| Best available | *nothing on the wire beats what you already have* |")
            lines.append("| Recommendation | **Hold.** Starting them is still your best option this week. |")
            continue

        fix = item.fix.player
        lines.append(f"| Best available | **{fix.name}** `{fix.team}` -- **{fix.points:.1f}** pts |")
        if item.bid:
            low, high = item.bid
            budget = f" of ${waiver_plan.remaining_budget} left" if waiver_plan and waiver_plan.remaining_budget is not None else ""
            lines.append(f"| Recommended bid | **${low}-${high}**{budget} |")
        lines.append(f"| Net gain | **+{item.gain:.1f} pts** |")
        if item.fix.trending_count:
            lines.append(f"| Market | added in **{item.fix.trending_count:,}** leagues recently -- expect competing bids |")

    return lines


# --- lineup ----------------------------------------------------------------

def _render_my_lineup_section(context, model):
    if context.my_roster is None:
        return ["", "No roster found for you in this league yet."]

    if context.projections_degraded:
        return ["", "**NO PROJECTIONS AVAILABLE** -- showing your currently-set Sleeper lineup only, not optimized.", ""] + [
            f"- {line}" for line in _current_starters_lines(context.my_roster, context.my_players)
        ]

    lines = []
    result = model.lineup
    lines.append("")
    lines.append(f"**Projected total: {result.total_points:.1f} pts** *(recommended lineup)*")
    lines.append("")

    scale = max([s.player.points for s in result.slots if s.player] + [1.0])
    lines.append("| Slot | Player | Proj | |")
    lines.append("|:--|:--|--:|:--|")
    for slot in result.slots:
        if slot.player is None:
            lines.append(f"| **{slot.slot}** | *(empty)* | -- | `{BAR_EMPTY * BAR_WIDTH}` |")
            continue
        lines.append(
            f"| **{slot.slot}** | {_player_cell(slot.player, context.players_raw)} "
            f"| **{slot.player.points:.1f}** | `{_bar(slot.player.points, scale)}` |"
        )

    lines.extend(_render_lineup_changes(model))

    if result.bench:
        lines.append("")
        lines.append("**Bench**")
        lines.append("")
        lines.append("| Player | Proj | |")
        lines.append("|:--|--:|:--|")
        for player in result.bench:
            lines.append(
                f"| {_player_cell(player, context.players_raw)} | {player.points:.1f} | `{_bar(player.points, scale)}` |"
            )

    return lines


def _render_lineup_changes(model):
    if not model.starts and not model.sits:
        return []

    lines = ["", "**Changes vs. your currently-set lineup**", ""]
    lines.append("| | Player |")
    lines.append("|:--|:--|")
    for player in model.starts:
        lines.append(f"| **Start** | {player.name} ({player.position}) -- {player.points:.1f} pts |")
    for player in model.sits:
        lines.append(f"| **Sit** | {player.name} ({player.position}) -- {player.points:.1f} pts |")
    if 0 < model.swap_gain < SWAP_NOISE_POINTS:
        lines.append("")
        lines.append(f"*Worth only +{model.swap_gain:.1f} pts -- inside projection noise. "
                     f"Make it if you're already in the app, skip it if you're not.*")
    return lines


# --- matchup ---------------------------------------------------------------

def _render_opponent_section(context, model):
    if context.opponent_roster is None:
        return ["", "No matchup data available yet."]

    lines = ["", f"Facing **{context.opponent_display_name}**."]

    if not context.projections_degraded:
        mine = model.my_total
        theirs = model.opponent_total or 0.0
        scale = max(mine, theirs, 1.0)
        lines.append("")
        lines.append("| | Projected | |")
        lines.append("|:--|--:|:--|")
        lines.append(f"| **You** | **{mine:.1f}** | `{_bar(mine, scale)}` |")
        lines.append(f"| **{context.opponent_display_name}** | **{theirs:.1f}** | `{_bar(theirs, scale)}` |")
        lines.append("")
        lines.append(margin_sentence(model) or "")

    lines.append("")
    lines.append(f"**{context.opponent_display_name}'s currently-set starters**")
    lines.append("")
    lines.append("| Player | Proj |")
    lines.append("|:--|--:|")
    starters = model.opponent_starters or _starters(context.opponent_roster, context.opponent_players)
    for player in starters:
        lines.append(f"| {player.name} ({player.position}, {player.team}) | {player.points:.1f} |")
    if not starters:
        lines.append("| *(no lineup set yet)* | -- |")
    return lines


# --- waivers ---------------------------------------------------------------

def _render_waiver_section(model):
    plan = model.plan
    lines = ["", f"*{plan.label}*"]

    if plan.degraded:
        lines.append("")
        lines.append("**NO PROJECTIONS AVAILABLE** -- ranked by roster need and trending-add velocity only.")

    if plan.faab and plan.remaining_budget is not None:
        lines.append("")
        lines.append(f"**Budget remaining: ${plan.remaining_budget}**")

    if not plan.targets:
        lines.append("")
        lines.append("No free agents found.")
        return lines

    # Two genuinely different reasons to add a player, previously mixed into
    # one list: someone who improves THIS week's lineup, and someone who
    # doesn't yet but whom the rest of the market is buying. Listing a
    # kicker worth +0.1 pts next to a QB worth +17.6 buried the second kind
    # and padded the brief with noise.
    upgrades = model.upgrades
    speculative = model.speculative

    lines.append("")
    lines.append("### Upgrades for this week")
    if not upgrades:
        lines.append("")
        lines.append("Nothing on the wire would improve your starting lineup this week.")
    else:
        by_position = upgrades_by_position(model)
        for position in by_position:
            lines.append("")
            lines.append(f"**{position}**")
            lines.append("")
            lines.append("| Player | Adds | Bid | Notes |" if plan.faab else "| Player | Adds | Notes |")
            lines.append("|:--|--:|:--|:--|" if plan.faab else "|:--|--:|:--|")
            for target in by_position[position]:
                lines.append(_waiver_row(target, plan))

    if speculative:
        lines.append("")
        lines.append("### Speculative adds")
        lines.append("")
        lines.append(
            "These wouldn't crack your lineup today, but the rest of the market is buying -- "
            "usually a role change or an injury ahead of them on the depth chart."
        )
        lines.append("")
        lines.append("| Player | Pos | Being added in |")
        lines.append("|:--|:--|--:|")
        for target in speculative:
            player = target.player
            lines.append(f"| {player.name} `{player.team}` | {player.position} | {target.trending_count:,} leagues |")

    if plan.pace_warning:
        lines.append("")
        lines.append("> [!IMPORTANT]")
        lines.append(f"> **Budget pace:** {plan.pace_warning}")
    if plan.priority_note:
        lines.append("")
        lines.append(f"**Priority:** {plan.priority_note}")

    return lines


def _waiver_row(target, plan):
    player = target.player
    adds = f"**+{target.marginal_value:.1f}**" if target.marginal_value > 0 else "--"
    note = f"trending in {target.trending_count:,} leagues" if target.trending_count else ""
    cells = [f"{player.name} `{player.team}`", adds]
    if plan.faab:
        low_high = plan.bids.get(player.player_id)
        cells.append(f"**${low_high[0]}-${low_high[1]}**" if low_high else "--")
    cells.append(note)
    return "| " + " | ".join(cells) + " |"


# --- shared helpers --------------------------------------------------------

def _bar(points, scale, width=BAR_WIDTH):
    filled = int(round(max(points, 0.0) / scale * width)) if scale > 0 else 0
    filled = min(filled, width)
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def _starters(roster, players):
    by_id = {p.player_id: p for p in players}
    return [by_id[pid] for pid in (roster.get("starters") or []) if pid != "0" and pid in by_id]


def _current_starters_lines(roster, players):
    by_id = {p.player_id: p for p in players}
    starter_ids = [pid for pid in roster.get("starters") or [] if pid != "0"]
    if not starter_ids:
        return ["*(no lineup set yet)*"]
    lines = []
    for player_id in starter_ids:
        player = by_id.get(player_id)
        lines.append(_player_line(player, {}) if player else f"(unknown player {player_id})")
    return lines


def _player_cell(player, players_raw):
    """Table-cell form: name, team chip, then any injury/depth flags."""
    flag = _injury_flag(player.player_id, players_raw)
    depth = _depth_chart_note(player.player_id, players_raw)
    return f"{player.name} `{player.team}`{flag}{depth}"


def _player_line(player, players_raw):
    flag = _injury_flag(player.player_id, players_raw)
    depth = _depth_chart_note(player.player_id, players_raw)
    extras = "".join(part for part in (flag, depth) if part)
    return f"{player.name} ({player.position}, {player.team}) -- {player.points:.1f} pts{extras}"


def _injury_flag(player_id, players_raw):
    status = (players_raw.get(player_id) or {}).get("injury_status")
    return f" **[{status}]**" if status in INJURY_STATUSES_WORTH_FLAGGING else ""


def _depth_chart_note(player_id, players_raw):
    player = players_raw.get(player_id) or {}
    position = player.get("depth_chart_position")
    order = player.get("depth_chart_order")
    if position and order and order > 1:
        return f" `depth: {position}{order}`"
    return ""
