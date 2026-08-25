from ffai.cache import Cache
from ffai.cli import build_parser, resolve_draft_slot
from ffai.config import LEAGUE_ID
from ffai.sleeper_client import SleeperAPIError


def test_draft_subcommand_parses_required_args():
    parser = build_parser()

    args = parser.parse_args(["draft", "draft123", "5"])

    assert args.command == "draft"
    assert args.draft_id == "draft123"
    assert args.draft_slot == "5"
    assert args.league_id == LEAGUE_ID


def test_draft_subcommand_accepts_league_id_override():
    parser = build_parser()

    args = parser.parse_args(["draft", "draft123", "5", "--league-id", "999"])

    assert args.league_id == "999"


def test_draft_subcommand_slot_is_optional():
    parser = build_parser()

    args = parser.parse_args(["draft", "draft123"])

    assert args.draft_id == "draft123"
    assert args.draft_slot is None


class FakeClient:
    def __init__(self, draft_data=None, user_data=None, raises=False):
        self.draft_data = draft_data
        self.user_data = user_data
        self.raises = raises

    def get_draft(self, draft_id):
        if self.raises:
            raise SleeperAPIError("simulated failure")
        return self.draft_data

    def get_user(self, username):
        if self.raises:
            raise SleeperAPIError("simulated failure")
        return self.user_data


def test_resolve_draft_slot_finds_slot_from_draft_order(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    client = FakeClient(
        draft_data={"draft_order": {"999888777": 4}},
        user_data={"user_id": "999888777"},
    )

    slot = resolve_draft_slot(client, cache, "draft123", "kevinkissedpeter")

    assert slot == "4"


def test_resolve_draft_slot_returns_none_when_order_not_set(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    client = FakeClient(
        draft_data={"draft_order": None},
        user_data={"user_id": "999888777"},
    )

    slot = resolve_draft_slot(client, cache, "draft123", "kevinkissedpeter")

    assert slot is None


def test_resolve_draft_slot_returns_none_when_user_not_in_order(tmp_path):
    cache = Cache(db_path=str(tmp_path / "cache.sqlite3"))
    client = FakeClient(
        draft_data={"draft_order": {"someone_else": 7}},
        user_data={"user_id": "999888777"},
    )

    slot = resolve_draft_slot(client, cache, "draft123", "kevinkissedpeter")

    assert slot is None


def test_startsit_subcommand_defaults():
    parser = build_parser()

    args = parser.parse_args(["startsit"])

    assert args.command == "startsit"
    assert args.league_id == LEAGUE_ID
    assert args.week is None


def test_startsit_subcommand_accepts_week_override():
    parser = build_parser()

    args = parser.parse_args(["startsit", "--week", "5"])

    assert args.week == 5


def test_brief_subcommand_defaults():
    parser = build_parser()

    args = parser.parse_args(["brief"])

    assert args.command == "brief"
    assert args.out is None


def test_brief_subcommand_accepts_out_path():
    parser = build_parser()

    args = parser.parse_args(["brief", "--out", "brief.md"])

    assert args.out == "brief.md"


def test_refresh_subcommand_defaults():
    parser = build_parser()

    args = parser.parse_args(["refresh"])

    assert args.command == "refresh"
    assert args.league_id == LEAGUE_ID


def test_waivers_subcommand_defaults():
    parser = build_parser()

    args = parser.parse_args(["waivers"])

    assert args.command == "waivers"
    assert args.league_id == LEAGUE_ID
    assert args.week is None


def test_waivers_subcommand_accepts_week_override():
    parser = build_parser()

    args = parser.parse_args(["waivers", "--week", "5"])

    assert args.week == 5


def test_trade_subcommand_parses_repeatable_send_and_receive():
    parser = build_parser()

    args = parser.parse_args([
        "trade", "--send", "Player One", "--send", "Player Two",
        "--receive", "Player Three", "--with", "Rival",
    ])

    assert args.command == "trade"
    assert args.send == ["Player One", "Player Two"]
    assert args.receive == ["Player Three"]
    assert args.with_manager == "Rival"
    assert args.league_id == LEAGUE_ID


def test_trade_subcommand_requires_send_receive_and_with():
    parser = build_parser()

    try:
        parser.parse_args(["trade", "--send", "Player One"])
        assert False, "expected SystemExit for missing required --receive/--with"
    except SystemExit:
        pass


def test_bench_report_subcommand_defaults():
    parser = build_parser()

    args = parser.parse_args(["bench-report"])

    assert args.command == "bench-report"
    assert args.league_id == LEAGUE_ID
    assert args.start_week == 1
    assert args.end_week is None


def test_bench_report_subcommand_accepts_week_range():
    parser = build_parser()

    args = parser.parse_args(["bench-report", "--start-week", "2", "--end-week", "5"])

    assert args.start_week == 2
    assert args.end_week == 5


def test_no_command_raises_system_exit():
    parser = build_parser()

    try:
        parser.parse_args([])
        assert False, "expected SystemExit for missing required subcommand"
    except SystemExit:
        pass
