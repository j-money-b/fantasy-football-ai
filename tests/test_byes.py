import json

from ffai.byes import load_bye_weeks


def test_load_bye_weeks_real_data_file():
    bye_weeks = load_bye_weeks()

    assert len(bye_weeks) == 32
    assert all(isinstance(week, int) for week in bye_weeks.values())
    assert "DET" in bye_weeks


def test_load_bye_weeks_missing_file_returns_empty_dict(tmp_path):
    missing_path = tmp_path / "does_not_exist.json"

    assert load_bye_weeks(path=missing_path) == {}


def test_load_bye_weeks_reads_given_path(tmp_path):
    path = tmp_path / "byes.json"
    path.write_text(json.dumps({"DET": 6}))

    assert load_bye_weeks(path=path) == {"DET": 6}
