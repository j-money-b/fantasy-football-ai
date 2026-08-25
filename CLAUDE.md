# CLAUDE.md

## What this project is

An AI fantasy football manager built on the Sleeper API. It advises — it does not take actions in Sleeper (the API is read-only, and this is also a deliberate scope boundary; see PRD.md Anti-Goals). Read `PRD.md` before making any design or architecture decision — it is the living spec and takes precedence over assumptions.

## League context

- **Real 2026 league ID:** `1389331182289719296` — "The Tush Pushers," confirmed 2026-08-24. This is now the default `LEAGUE_ID` in `ffai/config.py`. **10 teams total** (you + 9 other managers, not 11 -- correct earlier docs that said eleven).
- **Draft:** snake, 14 rounds, standard 10-team slots (QB/RB/RB/WR/WR/TE/FLEX/K/DEF/5 BN). Scheduled **2026-08-30, 13:00 ET** (`draft_id` `1389331182289719297`, currently `pre_draft`, `draft_order` not yet set). This is the hard deadline referenced throughout the PRD.
- **Scoring:** standard passing (4pt/TD, -1 INT), full PPR (1.0/rec), 6pt return/pick-six TDs, missed FG/XP penalties. Same scoring carried over from last season.
- **Waivers:** FAAB, $100 budget (`waiver_type: 2`), same as last year.
- **Keepers:** league settings still show `max_keepers: 1`, but the user believes keeping isn't actually in play this year. **Unconfirmed** -- verify with the commissioner before the draft. If keeping turns out to be active, the draft assistant needs work first: it currently ships with pure redraft VORP/tiers, no keeper-specific logic (see PRD Open Question #5).
- **Opponent scouting (PRD Open Question #4, resolved):** this league's `previous_league_id` is `1261164996717453313`, confirmed to be the same manager group's 2025 season ("The Tush Pushers," 10 teams, same scoring, FAAB). The user was not in that league. Real per-manager 2025 FAAB spend (`waiver_budget_used` / 100): two managers spent their full budget (owners `724450157927714816`, `199936627823353856`), one spent almost nothing (`2`, owner `1001643411927068672`), rest spread 10-53. Worth folding into waiver/FAAB reasoning once manager identities are mapped to this year's rosters.
- **Sandbox league ID:** `1391979274122035200` -- still a real Sleeper league (single-manager, full commissioner tools), now used purely for testing/dev rather than as the primary league. Override via `SLEEPER_LEAGUE_ID` env var to point commands at it instead of the real league.
- **Sleeper username:** `kevinkissedpeter`

## How to work with the user

The user is new to both fantasy football and Sleeper. Adjust communication accordingly:

- **Explain reasoning, don't just give answers.** When recommending a start/sit call, a waiver target, or a draft pick, say *why* — the injury note, the depth chart change, the matchup factor — not just the verdict. The user needs to build understanding well enough to disagree with a recommendation for a reason by mid-season (this is Tier 3 in the PRD's success criteria, not optional polish).
- **Walk through setup and multi-step processes one step at a time.** Don't dump a full checklist or wall of instructions. Give one step, let the user complete it, confirm, then give the next. This applies to things like connecting to the real league, configuring GitHub Actions, or working through any first-time setup.
- Assume no prior Sleeper vocabulary (FAAB, waiver priority, IDP, etc.) — define terms briefly the first time they come up rather than assuming familiarity.
- **Don't re-confirm actions a prior directive already implies.** If the user gives clear direction (e.g. "save this in the docs"), its natural follow-through (e.g. committing that doc change) doesn't need a separate "want me to commit?" check. This doesn't relax the general git-safety default of not committing/pushing unrequested changes out of nowhere — it's about not adding an extra confirmation loop for the follow-through of something already directed. Genuinely separate or riskier actions (force-push, deleting something) still warrant a check even after related direction.
