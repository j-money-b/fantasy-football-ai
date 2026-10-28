import re

from ffai.brief_model import build_brief_model
from ffai.email_brief import email_subject, render_brief_html
from datetime import datetime

from ffai.availability import apply_availability
from ffai.brief_model import EASTERN
from ffai.models import PlayerProjection
from ffai.roster_moves import MovePlan, RosterMove
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


def _plan(moves=(), faab=True, remaining=100):
    return WaiverPlan(
        targets=[], ranked=[], bids={}, label="FAAB Bidding", faab=faab,
        remaining_budget=remaining, pace_warning=None, priority_note=None, degraded=False,
        moves=MovePlan(weeks=[3, 4, 5, 6], moves=list(moves), wire={}, rising=[]),
    )


def _move(add, drop, bid=(20, 35), trending=559632):
    return RosterMove(
        add=add, drop=drop, gain=17.6, gain_this_week=17.6, weeks=[3, 4, 5, 6],
        add_by_week=[17.6, 16.0, 15.0, 14.0], drop_by_week=[0.0, 0.0, 0.0, 0.0],
        trending_count=trending, bid=bid, worth=40, market="heavy competition -- bid near full value",
        reasons=["Improves this week's lineup by 17.6 pts."],
    )


def _ruled_out_qb_context(**overrides):
    raw = {"1": {"injury_status": "Out", "injury_body_part": "Elbow"}}
    defaults = dict(
        roster_positions=["QB", "BN"],
        my_players=apply_availability([_player("1", "QB", 18.0), _player("2", "QB", 12.0)], raw, week=3),
        my_roster={"roster_id": 1, "starters": ["1"]},
        players_raw=raw,
    )
    defaults.update(overrides)
    return _context(**defaults)


TUESDAY = datetime(2026, 10, 27, 8, 0, tzinfo=EASTERN)


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

def test_html_names_the_pickup_the_drop_and_the_bid():
    plan = _plan([_move(_player("99", "QB", 17.6), _player("2", "RB", 1.0))])

    html = render_brief_html(_context(), waiver_plan=plan, now=TUESDAY)

    assert "Pickups" in html
    assert "QB-99" in html
    assert "bid $20-$35 before Wednesday ~3am ET" in html
    assert "+17.6 pts" in html
    assert "559,632 adds in the last day" in html


def test_html_benches_a_ruled_out_starter():
    html = render_brief_html(_ruled_out_qb_context())

    assert "Set your lineup" in html
    assert "QB-1 is Out (Elbow) and won&#x27;t score" in html


def test_html_strips_markdown_emphasis_from_shared_copy():
    # The shared model writes "**Start ...**" for the markdown renderer; the
    # HTML renderer styles emphasis itself and must not leak the asterisks.
    plan = _plan([_move(_player("99", "QB", 17.6), _player("2", "RB", 1.0))])

    html = render_brief_html(_ruled_out_qb_context(), waiver_plan=plan)

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

def test_subject_names_the_player_to_bench_and_the_pickup():
    plan = _plan([_move(_player("99", "QB", 17.6), _player("2", "QB", 12.0))])

    subject = email_subject(build_brief_model(_ruled_out_qb_context(), plan))

    assert subject == "Week 3: bench QB-1, add QB-99"


def test_subject_says_so_when_nothing_needs_doing():
    subject = email_subject(build_brief_model(_context(), _plan()))

    assert subject == "Week 3: lineup is set, nothing worth adding"


def test_subject_flags_a_degraded_run():
    subject = email_subject(build_brief_model(_context(projections_degraded=True)))

    assert "degraded" in subject


# --- inbox legibility ------------------------------------------------------
# Subject + preheader are what the inbox list shows. Together they have to
# answer "what do I do this week" without the mail being opened.

def test_preheader_carries_the_decision_not_boilerplate():
    html = render_brief_html(_ruled_out_qb_context())
    preheader = html.split('mso-hide:all;')[1].split(">", 1)[1].split("&#8203;")[0]

    assert "Start QB-2 instead of QB-1" in preheader
    assert "Out" in preheader


def test_todo_is_the_first_thing_after_the_masthead():
    # A brief read on a phone must not put a scoreboard above the answer.
    context = _ruled_out_qb_context(
        opponent_roster={"roster_id": 2, "starters": ["3"]},
        opponent_display_name="Rival",
        opponent_players=[_player("3", "WR", 30.0)],
    )

    html = render_brief_html(context)

    assert html.index("Do this") < html.index("projected to")


def test_headline_says_so_when_nothing_needs_doing():
    html = render_brief_html(_context(), waiver_plan=_plan())

    assert "All clear" in html
    assert "Nothing to do" in html


def test_apple_mail_is_told_not_to_invert_the_palette():
    html = render_brief_html(_context())

    assert 'name="color-scheme" content="light"' in html
    assert 'name="supported-color-schemes" content="light"' in html


def test_bench_separator_is_not_double_escaped():
    context = _context(roster_positions=["QB", "BN"])

    html = render_brief_html(context)

    assert "&amp;middot;" not in html
