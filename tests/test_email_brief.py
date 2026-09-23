import re

from ffai.brief_model import build_brief_model
from ffai.email_brief import email_subject, render_brief_html
from ffai.models import PlayerProjection, WaiverTarget
from ffai.waivers import WaiverPlan
from ffai.weekly_context import WeeklyContext


def _player(player_id, position, points, team="KC"):
    return PlayerProjection(
        player_id=player_id, name=f"{position}-{player_id}", position=position, team=team,
        bye_week=None, points=points, data_source="test",
    )


def _context(**overrides):
    defaults = dict(
        week=3, season="2026", league={"name": "The Tush Pushers"}, players_raw={},
        roster_positions=["QB", "RB", "BN"],
        my_roster={"roster_id": 1, "starters": ["1", "2"]},
        my_players=[_player("1", "QB", 20.0), _player("2", "RB", 15.0)],
        opponent_roster=None, opponent_display_name=None, opponent_players=[],
        projections_degraded=False, warnings=[],
    )
    defaults.update(overrides)
    return WeeklyContext(**defaults)


def _plan(targets=(), bids=None, faab=True, remaining=100):
    targets = list(targets)
    return WaiverPlan(
        targets=targets, ranked=targets, bids=bids or {}, label="FAAB Bidding", faab=faab,
        remaining_budget=remaining, pace_warning=None, priority_note=None, degraded=False,
    )


def _target(player, marginal_value=10.0, trending=None):
    return WaiverTarget(player=player, marginal_value=marginal_value, trending_count=trending, reasons=[])


# --- email client constraints ----------------------------------------------
# These are the rules that decide whether the brief renders or arrives as a
# wall of unstyled text, so they are asserted rather than trusted.

def test_html_carries_no_stylesheet_or_style_block():
    # Gmail strips <style> blocks in several contexts; every style must be
    # inline or it is not reliably applied.
    html = render_brief_html(_context())

    assert "<style" not in html.lower()
    assert "<link" not in html.lower()


def test_html_uses_no_webfonts_or_css_variables():
    html = render_brief_html(_context())

    assert "fonts.googleapis" not in html
    assert "@font-face" not in html
    assert "var(--" not in html


def test_html_uses_no_media_queries():
    # Unreliable across clients -- the design commits to one light theme.
    html = render_brief_html(_context())

    assert "@media" not in html


def test_bars_are_tables_not_divs_with_widths():
    # Outlook's Word engine renders no flex/grid; nested table cells with a
    # background colour are the one charting primitive that works everywhere.
    context = _context(
        opponent_roster={"roster_id": 2, "starters": ["3"]},
        opponent_display_name="Rival",
        opponent_players=[_player("3", "WR", 30.0)],
    )

    html = render_brief_html(context)

    assert 'role="presentation"' in html
    assert re.search(r'<td width="\d+%" style="background:', html)


def test_player_names_are_html_escaped():
    context = _context(my_players=[_player("1", "QB", 20.0), _player("2", "RB", 15.0)])
    context.my_players[0].name = "A&W <script>Jones</script>"

    html = render_brief_html(context)

    assert "<script>" not in html
    assert "A&amp;W" in html


# --- content ---------------------------------------------------------------

def test_html_names_the_replacement_and_the_bid():
    context = _context(
        roster_positions=["QB"], my_players=[_player("1", "QB", 0.0)],
        my_roster={"roster_id": 1, "starters": ["1"]},
        players_raw={"1": {"injury_status": "Out"}},
    )
    plan = _plan(targets=[_target(_player("99", "QB", 17.6), 17.6, trending=559632)], bids={"99": (20, 35)})

    html = render_brief_html(context, waiver_plan=plan)

    assert "Action required" in html
    assert "QB-99" in html
    assert "$20&ndash;$35" in html
    assert "+17.6 pts" in html
    assert "559,632 leagues" in html


def test_html_strips_markdown_emphasis_from_shared_copy():
    # The shared model writes "**Out**" for the markdown renderer; the HTML
    # renderer styles emphasis itself and must not leak the asterisks.
    context = _context(
        roster_positions=["QB"], my_players=[_player("1", "QB", 0.0)],
        my_roster={"roster_id": 1, "starters": ["1"]},
        players_raw={"1": {"injury_status": "Out"}},
    )

    html = render_brief_html(context)

    assert "**" not in html


def test_html_shows_the_staleness_banner():
    html = render_brief_html(_context(warnings=["league data is stale -- 3.0 hours old"]))

    assert "DO NOT FULLY TRUST THIS BRIEF" in html
    assert "league data is stale" in html


def test_html_degrades_without_projections():
    context = _context(projections_degraded=True)

    html = render_brief_html(context)

    assert "No projections available" in html


def test_html_handles_missing_roster():
    html = render_brief_html(_context(my_roster=None, my_players=[]))

    assert "No roster found for you in this league yet." in html


# --- subject line ----------------------------------------------------------

def test_subject_leads_with_the_critical_slot():
    context = _context(
        roster_positions=["QB"], my_players=[_player("1", "QB", 0.0)],
        my_roster={"roster_id": 1, "starters": ["1"]},
        players_raw={"1": {"injury_status": "Out"}},
    )

    subject = email_subject(build_brief_model(context))

    assert subject == "Week 3: fix your QB -- 1 critical"


def test_subject_says_so_when_nothing_needs_doing():
    subject = email_subject(build_brief_model(_context()))

    assert subject == "Week 3: lineup is set, no action needed"


def test_subject_flags_a_degraded_run():
    subject = email_subject(build_brief_model(_context(projections_degraded=True)))

    assert "degraded" in subject
