# CLAUDE.md

## What this project is

An AI fantasy football manager built on the Sleeper API. It advises — it does not take actions in Sleeper (the API is read-only, and this is also a deliberate scope boundary; see PRD.md Anti-Goals). Read `PRD.md` before making any design or architecture decision — it is the living spec and takes precedence over assumptions.

## League context

- **Sandbox league ID:** `1391979274122035200` — a throwaway dev-fixture league (Phase 0), used until the real 2026 league exists. Do not treat data from it as real league data.
- **Sleeper username:** `kevinkissedpeter`
- The real 2026 league ID is not yet known (Sleeper mints a new ID each season). Do not reuse a prior season's league ID.

## How to work with the user

The user is new to both fantasy football and Sleeper. Adjust communication accordingly:

- **Explain reasoning, don't just give answers.** When recommending a start/sit call, a waiver target, or a draft pick, say *why* — the injury note, the depth chart change, the matchup factor — not just the verdict. The user needs to build understanding well enough to disagree with a recommendation for a reason by mid-season (this is Tier 3 in the PRD's success criteria, not optional polish).
- **Walk through setup and multi-step processes one step at a time.** Don't dump a full checklist or wall of instructions. Give one step, let the user complete it, confirm, then give the next. This applies to things like connecting to the real league, configuring GitHub Actions, or working through any first-time setup.
- Assume no prior Sleeper vocabulary (FAAB, waiver priority, IDP, etc.) — define terms briefly the first time they come up rather than assuming familiarity.
