from ffai.cli import build_parser
from ffai.config import LEAGUE_ID


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


def test_no_command_raises_system_exit():
    parser = build_parser()

    try:
        parser.parse_args([])
        assert False, "expected SystemExit for missing required subcommand"
    except SystemExit:
        pass
