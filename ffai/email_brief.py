"""Email-safe HTML rendering of the weekly brief.

Written against EMAIL client constraints, not browser ones -- this is why it
does not reuse the artifact/browser styling:

  * Every style is INLINE. Gmail strips <style> blocks in several contexts
    (notably the mobile apps and forwarded mail), so a stylesheet cannot be
    relied on at all.
  * Layout is <table>, not flex/grid. Outlook's Word rendering engine has no
    support for either.
  * No CSS custom properties, no @media -- both are unreliable across clients,
    so the palette is hardcoded and the design commits to one light theme that
    stays legible if a client force-inverts it.
  * No webfonts. @font-face and <link> to a font host are widely stripped, so
    every family is a system stack.
  * Bars are nested table cells with a background colour and a percentage
    width, which is the one charting primitive that renders everywhere.

The CONTENT decisions all live in brief_model.py, shared with the markdown
renderer, so the emailed brief and the GitHub thread can never disagree about
what this week's recommendation actually is."""

from datetime import datetime, timezone
from html import escape

from ffai.brief_model import (
    SWAP_NOISE_POINTS,
    build_brief_model,
    depth_note,
    headline,
    margin_sentence,
    pickup_instruction,
    status_label,
    todo,
)
from ffai.availability import OUT_THIS_WEEK

INK = "#141A22"
INK_2 = "#47535F"
INK_3 = "#78848F"
LINE = "#D3DAE4"
LINE_SOFT = "#E4E9F0"
GROUND = "#EEF1F5"
PANEL = "#FFFFFF"
PANEL_2 = "#F6F8FB"
ACCENT = "#B87413"
ACCENT_BG = "#FBF0DC"
CRIT = "#C0402B"
CRIT_BG = "#FBE8E4"
GOOD = "#2F7A4E"
GOOD_BG = "#E4F1E8"
TRACK = "#DDE3EB"

SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
MONO = "'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"

CELL = f"padding:9px 20px;border-bottom:1px solid {LINE_SOFT};font-family:{SANS};font-size:14px;color:{INK};"
HEAD_CELL = (
    f"padding:9px 20px;border-bottom:1px solid {LINE_SOFT};font-family:{MONO};font-size:10px;"
    f"font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:{INK_3};text-align:left;"
)
NUM = f"font-family:{MONO};font-weight:600;text-align:right;white-space:nowrap;"


def render_brief_html(context, waiver_plan=None, now=None):
    model = build_brief_model(context, waiver_plan, now=now)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = []

    parts.append(_masthead(model, context, stamp))
    parts.extend(_warnings(model))

    if not model.has_roster:
        parts.append(_panel("Roster", _note("No roster found for you in this league yet.")))
        return _shell("".join(parts), model)

    if model.degraded:
        parts.append(_panel(
            "No projections available",
            _note("Showing your currently-set Sleeper lineup only -- not optimized.")
            + _simple_player_table(model.current_starters, context),
        ))
        return _shell("".join(parts), model)

    # Order is the order of decisions: what to do, then the lineup, then the
    # pickups and why, then the whole wire, and the scoreboard last -- a
    # brief read on a phone must not put the matchup above the actions.
    parts.append(_todo_panel(model))
    parts.extend(_lineup_changes_panel(model))
    parts.append(_lineup_panel(model, context))
    parts.append(_bench_panel(model, context))
    parts.extend(_move_cards(model))
    parts.extend(_wire_panels(model))
    parts.append(_scoreline(model))
    parts.extend(_opponent_panel(model, context))

    return _shell("".join(parts), model)



# --- chrome ----------------------------------------------------------------

def _shell(body, model):
    subject_hint = escape(email_subject(model))
    # Preheader: the grey preview text an inbox shows beside the subject. Left
    # unset, clients scrape whatever markup comes first, which is why so much
    # mail previews as "View in browser". The trailing zero-width joiners stop
    # body copy from bleeding in after it.
    preheader = escape(headline(model))
    # color-scheme:light is deliberate -- it stops Apple Mail force-inverting
    # a palette that was designed as a set, which turns considered colour into
    # mud. The design commits to one theme rather than fighting the client.
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>{subject_hint}</title>
</head>
<body style="margin:0;padding:0;background:{GROUND};-webkit-text-size-adjust:100%;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all;font-size:1px;line-height:1px;color:{GROUND};">{preheader}&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;&#8203;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{GROUND};">
<tr><td align="center" style="padding:24px 12px 48px;">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0" style="width:640px;max-width:100%;">
{body}
<tr><td style="padding:22px 4px 0;font-family:{SANS};font-size:11px;line-height:1.6;color:{INK_3};text-align:center;">
Generated from live Sleeper data by your own fantasy assistant.<br>
It advises only &mdash; nothing here has been done to your Sleeper team for you.
</td></tr>
</table>
</td></tr></table>
</body></html>"""


def _masthead(model, context, stamp):
    league = escape(str(context.league.get("name") or "Fantasy"))
    opponent = f" &middot; vs. <b>{escape(model.opponent_name)}</b>" if model.opponent_name else ""
    return f"""<tr><td style="padding:0 0 14px;border-bottom:2px solid {INK};">
<div style="font-family:{MONO};font-size:10px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:{INK_3};">{league}</div>
<div style="font-family:{SANS};font-size:30px;font-weight:700;color:{INK};letter-spacing:-.01em;line-height:1.1;padding-top:2px;">Week {model.week} Brief</div>
<div style="font-family:{SANS};font-size:13px;color:{INK_2};padding-top:4px;">Last successful run {stamp}{opponent}</div>
</td></tr>"""


def _warnings(model):
    if not model.warnings:
        return []
    rows = "".join(
        f'<div style="font-family:{SANS};font-size:13px;color:{INK_2};padding-top:4px;">&bull; {escape(w)}</div>'
        for w in model.warnings
    )
    return [f"""<tr><td style="padding-top:18px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{CRIT_BG};border-left:4px solid {CRIT};">
<tr><td style="padding:14px 18px;">
<div style="font-family:{SANS};font-size:14px;font-weight:700;color:{CRIT};">DATA MAY BE STALE OR INCOMPLETE -- DO NOT FULLY TRUST THIS BRIEF</div>
{rows}</td></tr></table></td></tr>"""]


# --- sections --------------------------------------------------------------

def _todo_panel(model):
    """The answer, before anything else: every action as a numbered list."""
    items = todo(model)
    if not items:
        return f"""<tr><td style="padding-top:18px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{GOOD_BG};border-left:4px solid {GOOD};">
<tr><td style="padding:15px 18px;">
{_kicker('All clear')}
<div style="font-family:{SANS};font-size:16px;line-height:1.45;font-weight:600;color:{INK};padding-top:4px;">{escape(headline(model))}</div>
</td></tr></table></td></tr>"""

    rows = ""
    for index, item in enumerate(items, start=1):
        rows += f"""<tr>
<td width="26" valign="top" style="font-family:{MONO};font-size:15px;font-weight:700;color:{ACCENT};padding:7px 0;">{index}.</td>
<td style="font-family:{SANS};font-size:15px;line-height:1.45;color:{INK};padding:7px 0;">{_md_bold(item)}</td></tr>"""
    window = ""
    if model.moves and model.window:
        window = f'<div style="font-family:{SANS};font-size:12.5px;line-height:1.5;color:{INK_2};padding-top:8px;border-top:1px solid {LINE};margin-top:6px;">{escape(model.window.text)}</div>'
    return f"""<tr><td style="padding-top:18px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{ACCENT_BG};border-left:4px solid {ACCENT};">
<tr><td style="padding:15px 18px;">
{_kicker('Do this')}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="padding-top:4px;">{rows}</table>
{window}
</td></tr></table></td></tr>"""


def _lineup_changes_panel(model):
    if not model.changes and not model.dead_slots and not model.locked:
        return []
    rows = ""
    for change in model.changes:
        if change.start:
            rows += _change_row("Start", GOOD, change.start)
        if change.sit:
            rows += _change_row("Bench", CRIT if change.sit.unavailable else INK_3, change.sit)
        rows += f'<tr><td></td><td colspan="2" style="padding:0 20px 12px 0;font-family:{SANS};font-size:13px;color:{INK_2};border-bottom:1px solid {LINE_SOFT};">{escape(change.reason)}</td></tr>'
    body = f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>' if rows else ""
    if 0 < model.swap_gain < SWAP_NOISE_POINTS:
        body += _note(f"Worth only +{model.swap_gain:.1f} pts -- inside projection noise. "
                      f"Make it if you're already in the app, skip it if you're not.")
    if model.dead_slots:
        fixing = any(m.gain_this_week > 0 for m in model.moves)
        body += _note(f"No one on your roster can score at {', '.join(model.dead_slots)} this week"
                      + (" -- see the pickups below for the fix." if fixing else " -- and nobody on the wire fills it."))
    if model.locked:
        body += _note("Already played this week and locked in place: " + ", ".join(p.name for p in model.locked) + ".")
    return [_panel("Set your lineup", body, tone=CRIT if any(c.sit and c.sit.unavailable for c in model.changes) else ACCENT, flush=True)]


def _change_row(label, color, player):
    return f"""<tr>
<td width="62" style="padding:10px 0 4px 20px;"><span style="font-family:{MONO};font-size:9.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;background:{color};color:#fff;padding:2px 6px;">{label}</span></td>
<td style="padding:10px 10px 4px 10px;font-family:{SANS};font-size:14px;color:{INK};">{escape(player.name)} <span style="font-family:{MONO};font-size:11px;color:{INK_3};">{escape(player.position)} &middot; {escape(player.team)}</span>{_status_chip(player.unavailable, True) if player.unavailable else ''}</td>
<td width="56" style="padding:10px 20px 4px 0;{NUM}font-size:14px;color:{INK};">{player.points:.1f}</td></tr>"""


def _lineup_panel(model, context):
    scale = max([s.player.points for s in model.lineup.slots if s.player] + [1.0])
    rows = ""
    for slot in model.lineup.slots:
        if slot.player is None:
            rows += (f'<tr><td style="{CELL}{MONO_SLOT()}">{escape(slot.slot)}</td>'
                     f'<td style="{CELL}color:{CRIT};" colspan="3"><i>nobody available</i></td></tr>')
            continue
        player = slot.player
        dead = player.points <= 0
        color = CRIT if dead else INK
        bar_color = CRIT if dead else (ACCENT if player.points >= scale else INK_3)
        rows += f"""<tr>
<td style="{CELL}{MONO_SLOT()}">{escape(slot.slot)}</td>
<td style="{CELL}">{_player_name(player, context)}</td>
<td width="56" style="{CELL}{NUM}color:{color};">{player.points:.1f}</td>
<td width="110" style="{CELL}">{_bar(player.points / scale, bar_color)}</td>
</tr>"""
    table = f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr><td style="{HEAD_CELL}">Slot</td><td style="{HEAD_CELL}">Player</td>
<td style="{HEAD_CELL}text-align:right;">Proj</td><td style="{HEAD_CELL}"></td></tr>
{rows}</table>"""
    return _panel("Best lineup", table, badge=f"{model.my_total:.1f} pts", flush=True)


def _bench_panel(model, context):
    if not model.lineup.bench:
        return ""
    scale = max([p.points for p in model.lineup.bench] + [1.0])
    rows = "".join(
        f'<tr><td style="{CELL}">{_player_name(player, context, with_position=True)}</td>'
        f'<td width="56" style="{CELL}{NUM}">{player.points:.1f}</td>'
        f'<td width="110" style="{CELL}">{_bar(player.points / scale, INK_3)}</td></tr>'
        for player in model.lineup.bench
    )
    return _panel("Bench", f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>', flush=True)


def _move_cards(model):
    plan = model.plan
    badge = f"${plan.remaining_budget} FAAB left" if plan is not None and plan.faab and plan.remaining_budget is not None else None
    span = _span(model)
    if not model.moves:
        parts = [_panel("Pickups", _note(
            f"No add/drop is worth making right now: nobody on the wire would improve your lineup by more than "
            f"a point a week over weeks {span}. The full scan is below."), badge=badge)]
        return parts + _pace_panel(plan)

    parts = [f"""<tr><td style="padding-top:22px;">
<span style="font-family:{SANS};font-size:18px;font-weight:700;color:{INK};">Pickups</span>
{f'<span style="font-family:{MONO};font-size:13px;font-weight:700;color:{INK};float:right;padding-top:4px;">{badge}</span>' if badge else ''}
</td></tr>"""]
    for index, move in enumerate(model.moves, start=1):
        add, drop = move.add, move.drop
        terms = _term("Do it", escape(pickup_instruction(model, move)), ACCENT)
        terms += _term(f"Gain, wk {span}", f"+{move.gain:.1f} pts", GOOD)
        if move.worth is not None and move.worth > 1:
            terms += _term("Max value", f"${move.worth}")
        why = "".join(
            f'<div style="font-family:{SANS};font-size:13.5px;line-height:1.5;color:{INK_2};padding:3px 0;">&bull; {escape(r)}</div>'
            for r in move.reasons
        )
        market = ""
        if move.market:
            adds = f" &middot; {move.trending_count:,} adds in the last day" if move.trending_count else ""
            market = f'<div style="font-family:{SANS};font-size:12.5px;color:{INK_3};padding-top:6px;">Market: {escape(move.market)}{adds}</div>'
        parts.append(f"""<tr><td style="padding-top:10px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{PANEL};border:1px solid {LINE};border-left:4px solid {GOOD};">
<tr><td style="padding:14px 16px 4px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td width="47%" valign="top" style="background:{GOOD_BG};padding:12px 14px;">
{_kicker(f'{index}. Add')}
<div style="font-family:{SANS};font-size:17px;font-weight:700;color:{INK};padding-top:3px;">{escape(add.name)}</div>
<div style="font-family:{SANS};font-size:12px;color:{INK_3};padding-top:2px;">{escape(add.position)} &middot; {escape(add.team)}{f' &middot; {escape(add.unavailable)} this week' if add.unavailable else ''}</div>
<div style="font-family:{MONO};font-size:12px;color:{GOOD};padding-top:6px;">{_by_week(move.weeks, move.add_by_week)}</div>
</td>
<td width="6%" align="center" style="font-family:{SANS};font-size:20px;color:{INK_3};">&harr;</td>
<td width="47%" valign="top" style="background:{CRIT_BG};padding:12px 14px;">
{_kicker('Drop')}
<div style="font-family:{SANS};font-size:17px;font-weight:700;color:{INK};padding-top:3px;">{escape(drop.name)}</div>
<div style="font-family:{SANS};font-size:12px;color:{INK_3};padding-top:2px;">{escape(drop.position)} &middot; {escape(drop.team)}{f' &middot; {escape(drop.unavailable)}' if drop.unavailable else ''}</div>
<div style="font-family:{MONO};font-size:12px;color:{CRIT};padding-top:6px;">{_by_week(move.weeks, move.drop_by_week)}</div>
</td></tr></table>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="padding-top:2px;"><tr>{terms}</tr></table>
</td></tr>
<tr><td style="padding:4px 16px 14px;">
{_kicker('Why')}
{why}{market}
</td></tr>
</table></td></tr>""")
    if len(model.moves) > 1:
        parts.append(f'<tr><td style="padding-top:6px;font-family:{SANS};font-size:12px;color:{INK_3};">Each move assumes you made the ones above it.</td></tr>')
    return parts + _pace_panel(plan)


def _pace_panel(plan):
    if plan is None or not plan.pace_warning:
        return []
    return [_panel("Budget pace", _note(_plain(plan.pace_warning)), tone=ACCENT)]


def _wire_panels(model):
    if not model.wire:
        return [_panel("On the wire right now", _note("No free agents found."))]
    span = _span(model)
    head = (f'<tr><td style="{HEAD_CELL}">Player</td><td style="{HEAD_CELL}text-align:right;">Pts wk {span}</td>'
            f'<td style="{HEAD_CELL}text-align:right;">Gain</td><td style="{HEAD_CELL}">Would replace</td></tr>')
    rows = ""
    for position, entries in model.wire.items():
        rows += f'<tr><td colspan="4" style="padding:10px 20px 4px;font-family:{MONO};font-size:10.5px;font-weight:700;letter-spacing:.1em;color:{INK_2};background:{PANEL_2};">{escape(position)}</td></tr>'
        rows += "".join(_wire_row(entry) for entry in entries)
    body = _note("The best available at each position. Gain is what he'd add to your team after cutting your "
                 "least useful player for him -- zero or less means he wouldn't help you.")
    body += f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{head}{rows}</table>'
    parts = [_panel("On the wire right now", body, flush=True)]

    if model.rising:
        rows = "".join(_wire_row(entry, show_adds=True) for entry in model.rising)
        head = (f'<tr><td style="{HEAD_CELL}">Player</td><td style="{HEAD_CELL}text-align:right;">Pts wk {span}</td>'
                f'<td style="{HEAD_CELL}text-align:right;">Gain</td><td style="{HEAD_CELL}">Adds today</td></tr>')
        body = _note("Who everyone else is adding -- usually a role change or an injury ahead of him on the depth chart.")
        body += f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{head}{rows}</table>'
        parts.append(_panel("Who everyone else is adding", body, flush=True))
    return parts


def _wire_row(entry, show_adds=False):
    p = entry.player
    gain_color = GOOD if entry.gain >= 1 else INK_3
    last = (f"{entry.trending_count:,}" if entry.trending_count else "&mdash;") if show_adds else (
        escape(entry.best_drop.name) if entry.best_drop else "&mdash;")
    chip = _status_chip(p.unavailable, True) if p.unavailable else ""
    return f"""<tr><td style="{CELL}">{escape(p.name)} <span style="font-family:{MONO};font-size:11px;color:{INK_3};">{escape(p.position)} &middot; {escape(p.team)}</span>{chip}</td>
<td width="70" style="{CELL}{NUM}">{entry.horizon_points:.1f}</td>
<td width="56" style="{CELL}{NUM}color:{gain_color};">{entry.gain:+.1f}</td>
<td style="{CELL}color:{INK_3};font-size:12.5px;">{last}</td></tr>"""


def _scoreline(model):
    if model.opponent_total is None:
        return ""
    scale = max(model.my_total, model.opponent_total, 1.0)
    rows = _score_row("You", model.my_total, model.my_total / scale, ACCENT)
    rows += _score_row(escape(model.opponent_name or "Opponent"), model.opponent_total,
                       model.opponent_total / scale, INK_3)
    sentence = _plain(margin_sentence(model) or "")
    return f"""<tr><td style="padding-top:18px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{PANEL};border:1px solid {LINE};">
<tr><td style="padding:12px 20px;background:{PANEL_2};border-bottom:1px solid {LINE_SOFT};"><span style="font-family:{SANS};font-size:15px;font-weight:700;color:{INK};">Matchup &mdash; Week {model.week}</span></td></tr>
<tr><td style="padding:18px 20px 6px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table></td></tr>
<tr><td style="padding:12px 20px 16px;"><div style="border-top:1px solid {LINE_SOFT};padding-top:12px;font-family:{SANS};font-size:14px;color:{INK_2};">{escape(sentence)} Yours is your best lineup; theirs is as currently set.</div></td></tr>
</table></td></tr>"""


def _score_row(name, value, fraction, color):
    return f"""<tr>
<td width="110" style="font-family:{SANS};font-size:15px;font-weight:700;color:{INK};padding:6px 10px 6px 0;">{name}</td>
<td style="padding:6px 10px 6px 0;">{_bar(fraction, color, height=13)}</td>
<td width="60" style="font-family:{MONO};font-size:18px;font-weight:700;color:{INK};text-align:right;padding:6px 0;">{value:.1f}</td>
</tr>"""


def _opponent_panel(model, context):
    if not model.opponent_starters:
        return []
    rows = "".join(
        f'<tr><td style="{CELL}{MONO_SLOT()}">{escape(p.position)}</td>'
        f'<td style="{CELL}">{_player_name(p, context)}</td>'
        f'<td width="56" style="{CELL}{NUM}">{p.points:.1f}</td></tr>'
        for p in model.opponent_starters
    )
    badge = f"{model.opponent_total:.1f} pts" if model.opponent_total is not None else None
    return [_panel(f"{escape(model.opponent_name or 'Opponent')}'s set lineup",
                   f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>',
                   badge=badge, flush=True)]


def _span(model):
    weeks = model.horizon_weeks or [model.week]
    return f"{weeks[0]}-{weeks[-1]}" if len(weeks) > 1 else str(weeks[0])


def _by_week(weeks, points):
    return " &middot; ".join(f"wk{w} {p:.1f}" for w, p in zip(weeks, points))


def _md_bold(text):
    """Renders the shared model's **bold** markdown as <b>, escaping the rest."""
    pieces = text.split("**")
    return "".join(f"<b>{escape(piece)}</b>" if i % 2 else escape(piece) for i, piece in enumerate(pieces))



# --- primitives ------------------------------------------------------------

def MONO_SLOT():
    return f"font-family:{MONO};font-size:11px;letter-spacing:.06em;color:{INK_2};width:56px;"


def _panel(title, body, badge=None, tone=None, flush=False):
    badge_html = (f'<span style="font-family:{MONO};font-size:13px;font-weight:700;color:{INK};float:right;">{badge}</span>'
                  if badge else "")
    border = f"border:1px solid {LINE};" + (f"border-left:4px solid {tone};" if tone else "")
    return f"""<tr><td style="padding-top:18px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{PANEL};{border}">
<tr><td style="padding:12px 20px;background:{PANEL_2};border-bottom:1px solid {LINE_SOFT};">
<span style="font-family:{SANS};font-size:15px;font-weight:700;color:{INK};">{title}</span>{badge_html}
</td></tr>
<tr><td style="padding:{'0' if flush else '14px 20px'};">{body}</td></tr>
</table></td></tr>"""


def _note(text):
    return f'<div style="font-family:{SANS};font-size:13.5px;color:{INK_2};padding:13px 20px;line-height:1.5;">{escape(text)}</div>'


def _kicker(text):
    return (f'<div style="font-family:{MONO};font-size:9.5px;font-weight:700;letter-spacing:.12em;'
            f'text-transform:uppercase;color:{INK_3};">{text}</div>')


def _term(label, value, color=None):
    return f"""<td valign="top" style="padding:12px 22px 4px 0;">
{_kicker(label)}
<div style="font-family:{MONO};font-size:15px;font-weight:700;color:{color or INK};padding-top:2px;">{value}</div></td>"""


def _bar(fraction, color, height=8):
    """A bar as nested table cells -- the one charting primitive that renders
    in every mail client, including Outlook's Word engine."""
    pct = max(0, min(100, int(round(fraction * 100))))
    filled = (f'<td width="{pct}%" style="background:{color};font-size:0;line-height:0;height:{height}px;">&nbsp;</td>'
              if pct > 0 else "")
    rest = (f'<td style="font-size:0;line-height:0;height:{height}px;">&nbsp;</td>' if pct < 100 else "")
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="background:{TRACK};height:{height}px;"><tr>{filled}{rest}</tr></table>')


def _player_name(player, context, with_position=False):
    bits = [f'<span style="font-weight:500;">{escape(player.name)}</span>']
    suffix = f"{escape(player.position)} &middot; {escape(player.team)}" if with_position else escape(player.team)
    bits.append(f'<span style="font-family:{MONO};font-size:11px;color:{INK_3};"> {suffix}</span>')
    label = status_label(player, context.players_raw)
    if label:
        status = (context.players_raw.get(player.player_id) or {}).get("injury_status")
        bits.append(_status_chip(label, bool(player.unavailable) or status in OUT_THIS_WEEK))
    depth = depth_note(player.player_id, context.players_raw)
    if depth:
        bits.append(f'<span style="font-family:{MONO};font-size:9px;color:{INK_3};border:1px solid {LINE};'
                    f'padding:1px 5px;margin-left:5px;">{escape(depth)}</span>')
    return "".join(bits)


def _status_chip(label, critical):
    bg, fg = (CRIT, "#ffffff") if critical else (ACCENT_BG, ACCENT)
    return (f'<span style="font-family:{MONO};font-size:9px;font-weight:700;letter-spacing:.08em;'
            f'text-transform:uppercase;background:{bg};color:{fg};padding:2px 5px;margin-left:6px;">{escape(label)}</span>')


def _simple_player_table(players, context):
    rows = "".join(f'<tr><td style="{CELL}">{_player_name(p, context, with_position=True)}</td></tr>' for p in players)
    if not rows:
        rows = f'<tr><td style="{CELL}color:{INK_3};"><i>no lineup set yet</i></td></tr>'
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>'


def _plain(text):
    """Strips the markdown emphasis the shared model writes for the markdown
    renderer -- the HTML renderer styles emphasis itself."""
    return text.replace("**", "")


def email_subject(model):
    """Front-loads the decision: an inbox preview should say whether action is
    needed without the mail being opened."""
    if model.degraded or not model.has_roster:
        return f"Week {model.week}: brief degraded -- check the data"
    parts = []
    benched_out = [c.sit for c in model.changes if c.sit and c.sit.unavailable]
    if benched_out:
        parts.append(f"bench {', '.join(p.name for p in benched_out)}")
    elif model.changes:
        parts.append(f"{len(model.changes)} lineup {'change' if len(model.changes) == 1 else 'changes'}")
    if model.moves:
        top = model.moves[0]
        more = f" +{len(model.moves) - 1} more" if len(model.moves) > 1 else ""
        parts.append(f"add {top.add.name}{more}")
    if not parts:
        return f"Week {model.week}: lineup is set, nothing worth adding"
    return f"Week {model.week}: " + ", ".join(parts)
