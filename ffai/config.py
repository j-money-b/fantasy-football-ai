import os

SLEEPER_BASE_URL = "https://api.sleeper.app/v1"

# Real 2026 league ("The Tush Pushers", PRD Open Question #1 -- resolved 2026-08-24).
# Override with SLEEPER_LEAGUE_ID to point at the sandbox league (1391979274122035200)
# for testing instead.
LEAGUE_ID = os.environ.get("SLEEPER_LEAGUE_ID", "1389331182289719296")

SLEEPER_USERNAME = os.environ.get("SLEEPER_USERNAME", "kevinkissedpeter")

CACHE_DB_PATH = os.environ.get("FFAI_CACHE_DB", "data/cache.sqlite3")

# Player dictionary (~5MB) is pulled at most this often (PRD 3.1: once daily, not per-request).
PLAYER_DICT_MAX_AGE_HOURS = int(os.environ.get("FFAI_PLAYER_DICT_MAX_AGE_HOURS", "24"))

# Projections adapter (PRD 4.2, R5 -- fragile dependency isolated to ffai/projections.py).
# Note: .com, not .app -- a different, undocumented host from the documented v1 API above.
SLEEPER_PROJECTIONS_HOST = os.environ.get("SLEEPER_PROJECTIONS_HOST", "https://api.sleeper.com")
ESPN_HOST = os.environ.get("ESPN_HOST", "https://lm-api-reads.fantasy.espn.com")
FANTASYPROS_HOST = os.environ.get("FANTASYPROS_HOST", "https://www.fantasypros.com")

# Draft assistant (PRD 6.1).
DRAFT_POLL_INTERVAL_SECONDS = int(os.environ.get("FFAI_DRAFT_POLL_INTERVAL_SECONDS", "5"))
