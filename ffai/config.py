import os

SLEEPER_BASE_URL = "https://api.sleeper.app/v1"

# Sandbox league (Phase 0 dev fixture). Swap by setting SLEEPER_LEAGUE_ID once
# the real 2026 league exists (PRD Open Question #1) -- no code change needed.
LEAGUE_ID = os.environ.get("SLEEPER_LEAGUE_ID", "1391979274122035200")

SLEEPER_USERNAME = os.environ.get("SLEEPER_USERNAME", "kevinkissedpeter")

CACHE_DB_PATH = os.environ.get("FFAI_CACHE_DB", "data/cache.sqlite3")
