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
    injury_status,
    margin_sentence,
    upgrades_by_position,
)

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


def render_brief_html(context, waiver_plan=None):
    model = build_brief_model(context, waiver_plan)
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

    parts.append(_headline_bar(model))
    parts.extend(_action_cards(model))
    parts.append(_scoreline(model))
    parts.append(_lineup_panel(model, context))
    parts.extend(_changes_panel(model))
    parts.extend(_waiver_panels(model))
    parts.append(_bench_panel(model, context))
    parts.extend(_opponent_panel(model))

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

def _headline_bar(model):
    """The answer, before anything else. A brief read on a phone must not put
    a scoreboard above the one sentence that says what to do."""
    text = escape(headline(model))
    urgent = any(item.hole.severity == "critical" for item in model.items)
    tone = CRIT if urgent else (ACCENT if model.items else GOOD)
    bg = CRIT_BG if urgent else (ACCENT_BG if model.items else GOOD_BG)
    label = "Do this" if model.items else "All clear"
    return f"""<tr><td style="padding-top:18px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{bg};border-left:4px solid {tone};">
<tr><td style="padding:15px 18px;">
{_kicker(label)}
<div style="font-family:{SANS};font-size:16px;line-height:1.45;font-weight:600;color:{INK};padding-top:4px;">{text}</div>
</td></tr></table></td></tr>"""


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
<tr><td style="padding:18px 20px 6px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table></td></tr>
<tr><td style="padding:12px 20px 16px;"><div style="border-top:1px solid {LINE_SOFT};padding-top:12px;font-family:{SANS};font-size:14px;color:{INK_2};">{escape(sentence)}</div></td></tr>
</table></td></tr>"""


def _score_row(name, value, fraction, color):
    return f"""<tr>
<td width="110" style="font-family:{SANS};font-size:15px;font-weight:700;color:{INK};padding:6px 10px 6px 0;">{name}</td>
<td style="padding:6px 10px 6px 0;">{_bar(fraction, color, height=13)}</td>
<td width="60" style="font-family:{MONO};font-size:18px;font-weight:700;color:{INK};text-align:right;padding:6px 0;">{value:.1f}</td>
</tr>"""


def _action_cards(model):
    if not model.items:
        return [_panel("Lineup check", _note(
            "No lineup holes. Every starting slot is filled by someone playing this week."), tone=GOOD)]

    parts = []
    count = len(model.items)
    parts.append(f"""<tr><td style="padding-top:18px;">
<div style="font-family:{SANS};font-size:18px;font-weight:700;color:{INK};">
Action required &mdash; {count} lineup {'hole' if count == 1 else 'holes'} to fix</div></td></tr>""")

    for item in model.items:
        hole = item.hole
        tone = CRIT if hole.severity == "critical" else ACCENT
        tone_bg = CRIT_BG if hole.severity == "critical" else ACCENT_BG
        now_name = escape(hole.player.name) if hole.player else f"nothing in your {escape(hole.slot)} slot"
        now_team = escape(hole.player.team) if hole.player else ""
        reason = escape(_plain(hole.reason))

        if item.fix is None:
            body = _note("Nothing on the wire beats what you already have. Hold -- starting them "
                         "is still your best option this week.")
        else:
            fix = item.fix.player
            bid_row = ""
            if item.bid:
                budget = f" of ${model.plan.remaining_budget} left" if model.plan and model.plan.remaining_budget is not None else ""
                bid_row = _term("Recommended bid", f"${item.bid[0]}&ndash;${item.bid[1]}{budget}", ACCENT)
            market = _term("Market pressure", f"{item.fix.trending_count:,} leagues") if item.fix.trending_count else ""
            body = f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr>
<td width="47%" valign="top" style="background:{CRIT_BG};padding:13px 15px;">
{_kicker('Starting now')}
<div style="font-family:{SANS};font-size:17px;font-weight:700;color:{INK};padding-top:3px;">{now_name}</div>
<div style="font-family:{SANS};font-size:12px;color:{INK_3};padding-top:2px;">{now_team}</div>
<div style="font-family:{MONO};font-size:22px;font-weight:700;color:{CRIT};padding-top:6px;">{hole.points:.1f}</div>
</td>
<td width="6%" align="center" style="font-family:{SANS};font-size:20px;color:{INK_3};">&rarr;</td>
<td width="47%" valign="top" style="background:{GOOD_BG};padding:13px 15px;">
{_kicker('Best available')}
<div style="font-family:{SANS};font-size:17px;font-weight:700;color:{INK};padding-top:3px;">{escape(fix.name)}</div>
<div style="font-family:{SANS};font-size:12px;color:{INK_3};padding-top:2px;">{escape(fix.team)} &middot; free agent</div>
<div style="font-family:{MONO};font-size:22px;font-weight:700;color:{GOOD};padding-top:6px;">{fix.points:.1f}</div>
</td></tr></table>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="padding-top:4px;"><tr>
{bid_row}{_term("Net gain", f"+{item.gain:.1f} pts", GOOD)}{market}
</tr></table>"""

        parts.append(f"""<tr><td style="padding-top:10px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{PANEL};border:1px solid {LINE};border-left:4px solid {tone};">
<tr><td style="padding:13px 18px;border-bottom:1px solid {LINE_SOFT};background:{tone_bg};">
<span style="font-family:{MONO};font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:{tone};">{escape(hole.slot)}</span>
<span style="font-family:{SANS};font-size:15px;font-weight:600;color:{INK};padding-left:8px;">{reason}</span>
</td></tr>
<tr><td style="padding:14px 16px;">{body}</td></tr>
</table></td></tr>""")
    return parts


def _lineup_panel(model, context):
    scale = max([s.player.points for s in model.lineup.slots if s.player] + [1.0])
    rows = ""
    for slot in model.lineup.slots:
        if slot.player is None:
            rows += (f'<tr><td style="{CELL}{MONO_SLOT()}">{escape(slot.slot)}</td>'
                     f'<td style="{CELL}color:{INK_3};" colspan="3"><i>empty</i></td></tr>')
            continue
        player = slot.player
        dead = player.points < 1.0 or injury_status(player.player_id, context.players_raw) in ("Out", "IR", "PUP", "Suspended")
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
    return _panel("Recommended lineup", table, badge=f"{model.my_total:.1f} pts", flush=True)


def _changes_panel(model):
    if not model.starts and not model.sits:
        return []
    rows = ""
    for player in model.starts:
        rows += _change_row("Start", GOOD, player)
    for player in model.sits:
        rows += _change_row("Sit", INK_3, player)
    body = f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>'
    if 0 < model.swap_gain < SWAP_NOISE_POINTS:
        body += _note(f"Worth only +{model.swap_gain:.1f} pts -- inside projection noise. "
                      f"Make it if you're already in the app, skip it if you're not.")
    return [_panel("Changes vs. your set lineup", body, flush=True)]


def _change_row(label, color, player):
    return f"""<tr>
<td width="62" style="{CELL}"><span style="font-family:{MONO};font-size:9.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;background:{color};color:#fff;padding:2px 6px;">{label}</span></td>
<td style="{CELL}">{escape(player.name)} <span style="font-family:{MONO};font-size:11px;color:{INK_3};">{escape(player.position)} &middot; {escape(player.team)}</span></td>
<td width="56" style="{CELL}{NUM}">{player.points:.1f}</td></tr>"""


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


def _waiver_panels(model):
    if model.plan is None:
        return []
    parts = []
    badge = f"${model.plan.remaining_budget} left" if model.plan.faab and model.plan.remaining_budget is not None else None

    if model.upgrades:
        body = ""
        for position, targets in upgrades_by_position(model).items():
            head = f'<tr><td style="{HEAD_CELL}">{escape(position)}</td><td style="{HEAD_CELL}text-align:right;">Adds</td>'
            head += f'<td style="{HEAD_CELL}">Bid</td>' if model.plan.faab else ""
            head += f'<td style="{HEAD_CELL}">Market</td></tr>'
            rows = ""
            for target in targets:
                bid = ""
                if model.plan.faab:
                    low_high = model.plan.bids.get(target.player.player_id)
                    text = f"${low_high[0]}&ndash;${low_high[1]}" if low_high else "&mdash;"
                    bid = f'<td style="{CELL}font-family:{MONO};font-weight:600;color:{ACCENT};">{text}</td>'
                market = f"{target.trending_count:,} leagues" if target.trending_count else "&mdash;"
                rows += f"""<tr><td style="{CELL}">{escape(target.player.name)} <span style="font-family:{MONO};font-size:11px;color:{INK_3};">{escape(target.player.team)}</span></td>
<td width="60" style="{CELL}{NUM}color:{GOOD};">+{target.marginal_value:.1f}</td>{bid}
<td style="{CELL}color:{INK_3};font-size:12.5px;">{market}</td></tr>"""
            body += f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{head}{rows}</table>'
        parts.append(_panel("Waiver wire &mdash; upgrades", body, badge=badge, flush=True))
    else:
        parts.append(_panel("Waiver wire", _note("Nothing on the wire would improve your starting lineup this week."), badge=badge))

    if model.speculative:
        rows = "".join(
            f'<tr><td style="{CELL}">{escape(t.player.name)} <span style="font-family:{MONO};font-size:11px;color:{INK_3};">{escape(t.player.position)} &middot; {escape(t.player.team)}</span></td>'
            f'<td style="{CELL}{NUM}color:{INK_3};font-weight:400;font-size:12.5px;">{t.trending_count:,} leagues</td></tr>'
            for t in model.speculative
        )
        body = _note("These wouldn't crack your lineup today, but the rest of the market is buying -- "
                     "usually a role change or an injury ahead of them on the depth chart.")
        body += f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>'
        parts.append(_panel("Speculative adds", body, flush=True))

    if model.plan.pace_warning:
        parts.append(_panel("Budget pace", _note(_plain(model.plan.pace_warning)), tone=ACCENT))
    if model.plan.priority_note:
        parts.append(_panel("Waiver priority", _note(_plain(model.plan.priority_note)), tone=ACCENT))
    return parts


def _opponent_panel(model):
    if not model.opponent_starters:
        return []
    rows = "".join(
        f'<tr><td style="{CELL}{MONO_SLOT()}">{escape(p.position)}</td>'
        f'<td style="{CELL}">{escape(p.name)} <span style="font-family:{MONO};font-size:11px;color:{INK_3};">{escape(p.team)}</span></td>'
        f'<td width="56" style="{CELL}{NUM}">{p.points:.1f}</td></tr>'
        for p in model.opponent_starters
    )
    badge = f"{model.opponent_total:.1f} pts" if model.opponent_total is not None else None
    return [_panel(f"{escape(model.opponent_name or 'Opponent')}'s set lineup",
                   f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}</table>',
                   badge=badge, flush=True)]


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
    suffix = f"{player.position} &middot; {player.team}" if with_position else player.team
    bits.append(f'<span style="font-family:{MONO};font-size:11px;color:{INK_3};"> {escape(suffix)}</span>')
    status = injury_status(player.player_id, context.players_raw)
    if status:
        crit = status in ("Out", "IR", "PUP", "Suspended")
        bg, fg = (CRIT, "#ffffff") if crit else (ACCENT_BG, ACCENT)
        bits.append(f'<span style="font-family:{MONO};font-size:9px;font-weight:700;letter-spacing:.08em;'
                    f'text-transform:uppercase;background:{bg};color:{fg};padding:2px 5px;margin-left:6px;">{escape(status)}</span>')
    depth = depth_note(player.player_id, context.players_raw)
    if depth:
        bits.append(f'<span style="font-family:{MONO};font-size:9px;color:{INK_3};border:1px solid {LINE};'
                    f'padding:1px 5px;margin-left:5px;">{escape(depth)}</span>')
    return "".join(bits)


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
    critical = [i for i in model.items if i.hole.severity == "critical"]
    if critical:
        slots = "/".join(dict.fromkeys(i.hole.slot for i in critical))
        return f"Week {model.week}: fix your {slots} -- {len(critical)} critical"
    if model.items:
        return f"Week {model.week}: {len(model.items)} lineup {'tweak' if len(model.items) == 1 else 'tweaks'}"
    return f"Week {model.week}: lineup is set, no action needed"
