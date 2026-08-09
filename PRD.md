# PRD — AI Fantasy Football Manager (Sleeper)

**Version:** 0.2
**Date:** August 8, 2026 (Phase 1 completed August 9, 2026; Phase 2 completed August 9, 2026)
**Season:** 2026 NFL
**Platform:** Sleeper
**Status:** Phase 2 code complete and pushed (weekly brief, start/sit, GitHub Actions scheduling, `refresh` escape hatch) — see §8 for two still-open verification items (real cron firing, live rostered-team test) before calling it fully done. Phase 3 (waivers + FAAB, trade evaluation, bench-points-lost tracking) not yet started.

---

## 1. Problem & Goals

### Problem

Managing a fantasy football team well requires consistent weekly attention: setting an optimal lineup, monitoring injury and depth-chart changes, identifying waiver targets before the rest of the league, and pricing FAAB bids correctly. Most managers do this from memory and vibes, on a phone, five minutes before kickoff. The information needed to do it well is public and free, but scattered and tedious to assemble by hand every week.

The user is new to this league and new to Sleeper (previously played on another platform), joining eleven managers who have played together before. There is no personal league history to draw on.

### What Winning Looks Like

Four tiers, ordered by how much they actually matter:

**Tier 1 — It works on Tuesday.** The floor and the primary requirement. When the user opens the weekly brief mid-workday, it is fresh, accurate, and actionable. A brief that silently fails or serves stale data as if it were current is the worst outcome in this document.

**Tier 2 — It beats the lazy baseline.** The comparison is not against an expert manager; it is against the same user without the tool (set lineup Sunday morning, grab whoever's obviously hot, ignore FAAB strategy). Two instrumentable metrics:

- **Points left on the bench** — weekly delta between the started lineup and the retrospectively optimal lineup. Computed exactly from Sleeper data, no judgment involved. **This is the headline metric.** Trending down over the season means the start/sit engine is earning its keep.
- **Waiver hit rate** — of recommended targets acquired, what fraction produced startable value. Noisy before Week 8.

**Tier 3 — The user understands their own team.** By mid-season the user should be able to disagree with a recommendation *for a reason*. The conversational layer exists for this.

**Tier 4 — Make the playoffs, win the league.** The actual ambition. Deliberately ranked last **as a metric**, not as a goal: a single fantasy season is too high-variance to evaluate tooling. Play to win; judge the tool on Tiers 1–3.

### Anti-Goals

- **Confidently wrong.** Plausible, well-formatted recommendations built on stale or malformed data. Worse than a crash, because the user acts on them.
- **Maintenance burden.** If keeping this alive costs more than a few minutes a week, it has negative value versus just reading FantasyPros.
- **Taking actions in Sleeper.** Out of scope. The tool advises; the user clicks. (Also technically enforced — the Sleeper API is read-only.)

---

## 2. Scope

### In Scope

1. Live draft assistant
2. Weekly start/sit recommendations
3. Waiver wire targets + FAAB bid guidance
4. Trade evaluation
5. Automated weekly brief
6. Conversational Q&A over live league data

### Out of Scope (v1)

- Executing any transaction in Sleeper
- Multi-league support (single league)
- Dynasty/keeper valuation (unless league format requires it — TBD)
- Mobile app or web UI
- Any paid data source
- Season-long simulation / playoff odds modeling

---

## 3. Architecture

A local Python project operated by Claude Code. Not a web app, not a hosted service.

### 3.1 Data Layer

- Thin Sleeper API client (`https://api.sleeper.app/v1`)
- SQLite cache
- Player dictionary (~5MB) pulled **once daily**, not per-request
- League/roster/matchup data pulled live (cheap, small)
- Every successful fetch written to cache as last-known-good

### 3.2 CLI Commands

| Command | Purpose |
|---|---|
| `brief` | Generate the weekly markdown brief |
| `draft` | Live draft assistant |
| `startsit` | Lineup recommendations for current week |
| `waivers` | Ranked targets + bid guidance |
| `trade` | Evaluate a proposed trade |
| `refresh` | **Escape hatch.** Force fresh pull, bypass all caches, print plain-text waiver summary |

Each command does data work deterministically and emits structured output. No LLM in the data path.

### 3.3 Conversational Layer

A `CLAUDE.md` at repo root describing the league, the user's roster, and how to invoke the commands. This is what makes "come in and ask questions" work — Claude Code reads it, runs the right command, and answers in plain English grounded in real output rather than guessing.

### 3.4 Scheduling

**GitHub Actions.** Private repo, scheduled workflow. Removes the user's laptop from the critical path entirely — the user does not keep it powered on.

- **Private, not public** (revised from an earlier draft of this doc). Private repos get 2,000 free Actions minutes/month, comfortably enough for two runs a week — no reason to expose league/roster data and config just to run a scheduled job.
- Runs Tuesday and Sunday mornings (~13:00 UTC ≈ 8–9am ET depending on DST — a single cron entry, not worth chasing the exact DST offset): Tuesday for post-waiver-processing planning context, Sunday for a final lineup-lock check.
- Known caveat: scheduled Actions can be delayed under load (minutes to occasionally longer). Acceptable for a morning brief.
- Workflow writes the brief back to the repo (`.github/workflows/weekly_brief.yml`, `brief.md`).
- Rejected: local cron (fails silently when the machine is asleep — this is the exact failure pattern the user has been burned by before).
- Deferred: VPS or scheduled cloud function (~$5/mo) if tighter timing near the waiver deadline is ever needed.

---

## 4. Data Sources

### 4.1 Sleeper API — Primary, Documented, Free

No auth, no API token, read-only. Rate limit guidance: stay under 1000 calls/min. Documented and stable for years.

Provides: league settings & scoring rules, rosters, league users, weekly matchups and scores, transactions, draft picks (live), full NFL player dictionary (team, position, depth chart order, injury status), trending adds/drops.

**This is the backbone.** Treated as reliable.

### 4.2 Projections — Adapter Pattern, Fallback Required

Sleeper's documented API has no projections endpoint. This is the only genuinely uncertain dependency.

**Risk statement (stated accurately):** free projection *data* is good — FantasyPros aggregates a wide expert set and is close to an industry standard; nflverse has real institutional backing. The risk is not data quality, it is that **every free programmatic path to that data is controlled by someone who has not committed to keeping it stable.** Sleeper's own projections sit on an undocumented endpoint. Scraper libraries break when site HTML changes.

**Mitigation:** one adapter, one interface, one file. Swapping sources is a contained change.

- Primary: Sleeper projections feed (undocumented)
- Fallback: ESPN endpoints
- **Fallback is season-only, not week-aware** (learned in Phase 2): ESPN's weekly-projection payload shape is undocumented and unverified, unlike its season aggregate (spot-checked in Milestone 0). Rather than guess at that shape and risk silently returning a player's season total mislabeled as their week's projection — a direct "confidently wrong" violation — the adapter refuses weekly ESPN fetches outright (`ffai/projections.py::_fetch_espn` raises immediately when `week` is set) and lets R2/R3 degrade honestly instead. Net effect: during the season, if Sleeper's projections feed is ever down or fails validation, weekly projections have no real fallback and the brief/start-sit correctly shows `NO PROJECTIONS AVAILABLE` rather than silently wrong numbers. Season-long fetches (draft prep) are unaffected.
- Supplementary cross-check (not a fallback): FantasyPros' free Projections pages (`fantasypros.com/nfl/projections/{pos}.php`) expose raw per-stat data unauthenticated, but only the **top 10 players per position** — the rest sits behind a registration fence (confirmed empirically; an earlier read of this page overstated what was open). Too thin to serve as a real fallback for the whole draft pool (would fail any reasonable coverage check), but exactly covers the early-round range where projection quality matters most and a second opinion is worth having. Used in the draft assistant's reasoning output for early picks only, not folded into the primary raw-stats pipeline.
- Validation baseline: nflverse historical actuals

**Proportionality note:** projections are the *least* decisive input in the system. Every source agrees the user should start their RB1. The edge comes from injury status, trending adds, depth chart movement, and opponent lineup holes — all of which are documented and free. Projections are a tiebreaker on close calls. Build the fallback; do not treat this as a crisis.

---

## 5. Scoring Engine

**Core decision: project raw stats, then apply the league's own scoring settings.**

Free sources publish fantasy points computed under *their* scoring rules, which will not match this league. Instead:

1. Pull projected raw stats (receptions, yards, TDs, etc.)
2. Pull league scoring settings from the Sleeper league object
3. Compute points under actual league rules

Consequences:
- Correct numbers for any format (PPR / half / standard / custom) with zero configuration
- League settings are **auto-discovered** — no need to interview the user about roster slots or scoring
- Projection source becomes a swappable adapter rather than a coupled dependency

**Validation:** replay 2025 actuals through the engine and confirm it reproduces known real point totals.

---

## 6. Features

### 6.1 Live Draft Assistant *(highest priority — hard deadline)*

- Polls the league's draft pick feed during a live draft
- Tracks board state: who's gone, who remains
- Recommends the pick with reasoning
- Tier-based board built from **league-specific** scoring, not generic rankings
- Value-over-replacement calculation
- Positional run detection
- Bye-week and stacking awareness

**Testing:** Sleeper mock drafts produce real draft IDs with real pick feeds, identical in shape to live drafts. This fully solves the "untestable until draft night" problem. Run repeatedly against mocks before the real draft.

**Implementation note (learned running a real mock draft in Phase 1):** the picks feed's `roster_id` field came back `null` for every pick in a `league_mock`-type draft — mock drafts aren't tied to real league rosters, so there's no roster to assign. `draft_slot` (the 1-N seat position) is populated reliably in both mock and real drafts and is what the assistant uses to track "my picks." Confirmed endpoint shapes: `GET /v1/draft/{id}` and `GET /v1/draft/{id}/picks` behave identically for mock and real drafts, picks are ordered/append-only by `pick_no`.

### 6.2 Weekly Start/Sit

- Optimal lineup for the week given roster and league slot configuration
- Reasoning per recommendation, not bare numbers
- Injury status and depth chart changes surfaced
- Opponent's lineup for context

### 6.3 Waivers + FAAB

**The league's waiver system is not yet known** — the commissioner sets it. Read from league settings; support both:

**If FAAB (most likely):** blind auction, fixed season budget (typically $100), no replenishment, processes overnight Tue→Wed, highest bid wins, ties usually break by roster order.
- Output: ranked targets with recommended **bid ranges** and reasoning
- Track remaining budget and pace — flag both over-conservatism (unspent budget at season end is pure waste) and early overspending

**If rolling priority:** queue-based, worst record on top, claim drops you to the back.
- Output: ranked targets plus a judgment on whether each is worth the user's current queue position

**Both paths must degrade gracefully:** with zero projections available, still rank by roster need and trending-add velocity. That is most of the signal regardless.

### 6.4 Trade Evaluation

- Value both sides under league scoring
- Account for roster construction, positional need, bye weeks
- Rest-of-season outlook, not just current-week value

---

## 7. Reliability Requirements

These are **hard requirements**, not polish. They exist because the user has had prior automations fail silently.

### R1 — Never fail silently
Every run writes a timestamped status. Any brief generated from stale data carries an **unmissable banner at the top** stating the data age (e.g. `⚠️ GENERATED FROM DATA 3 DAYS OLD — DO NOT TRUST`). A missing brief is ambiguous; a brief that announces its own staleness is not.

### R2 — Tiered degradation
Never crash out. If projections are unavailable, still emit roster, injury statuses, opponent lineup, and trending adds, explicitly labeled `NO PROJECTIONS AVAILABLE`. Partial output beats a stack trace.

### R3 — Last-known-good cache
Every successful fetch persisted. Failed fetch serves cached data **with staleness warning attached**. This alone eliminates most Tuesday-morning failure scenarios.

### R4 — Validate before trusting
On every projection pull: payload size within expected range, values within plausible bounds, every rostered starter received a number. Failed check → fall back, do not proceed.

### R5 — Isolate the fragile dependency
Projections adapter behind a single interface in a single file.

### R6 — Heartbeat
Scheduled job writes a completion timestamp. Brief displays `Last successful run: <timestamp>` at the top. The user can tell at a glance whether they are reading fresh output or a corpse.

### R7 — Manual escape hatch
`refresh` command: forces a fresh pull, bypasses all caches, depends on no scheduler having run correctly, prints a plain-text waiver summary. Runnable from anywhere. **This is the thing that saves the bad Tuesday.**

### Honest caveat
None of this makes the system bulletproof. The realistic promise: failures are loud, partial output remains useful, and the fragile component is quarantined.

---

## 8. Build Phases

Sequenced against a hard draft deadline of 2–4 weeks out.

### Phase 0 — Done
- Repo skeleton, Sleeper client, SQLite cache
- Confirm league read works end-to-end
- **Dev fixture:** user creates a real Sleeper league (free, instant, ~12 team / half-PPR, no other members needed) to provide a real league object with real settings and a real draft ID. This is a genuine league with full commissioner functionality (not a synthetic/limited mock) — it's just single-manager and not the actual 2026 league with the other 11 managers. Whether it's kept or deleted once the real league exists is still an open call, not yet decided.

### Phase 1 — Done (2026-08-09)
- Scoring engine (`ffai/scoring.py`) — validated to an exact match against real 2025 Sleeper actual-stats data across standard/half/PPR
- Value-over-replacement tiers (`ffai/vorp.py`) — FLEX-slot-aware replacement levels, gap-based tier clustering
- Projections adapter (`ffai/projections.py`, R5) — Sleeper (primary) -> ESPN (fallback) with R4 validation, plus a FantasyPros top-10-per-position consensus cross-check used for draft reasoning only (not a fallback -- their free tier only exposes the top 10 per position)
- Live draft assistant (`ffai/draft_assistant.py`, `ffai/cli.py draft`) — polls a live draft, tracks the user's picks via `draft_slot`, recommends by VORP with tier/roster-need/bye-collision/positional-run/stacking/consensus reasoning, degrades gracefully to roster-need-only ranking if projections are fully unavailable
- **Validated end-to-end against a real Sleeper mock draft** (12-team PPR, full 15 rounds) run in the sandbox league — caught and fixed two real bugs in the process: `roster_id` is null in mock drafts (see 6.1 implementation note) and an overly broad "known player" set was failing the R4 coverage check on genuinely good data
- 111 tests passing (`python -m pytest -q`)
- Follow-up noted for next mock draft session: confirm the 5s poll interval (`DRAFT_POLL_INTERVAL_SECONDS`) feels responsive enough for live back-and-forth; plenty of rate-limit headroom to tighten it if not

### Phase 2 — Done (2026-08-09)
- Optimal-lineup engine (`ffai/lineup.py`) — exact maximum-weight assignment (greedy-by-points + augmenting-path search over slots), not an approximation; needed because the eventual "points left on the bench" metric (Tier 2 headline) requires the optimal lineup computed exactly, no judgment involved. Reused as-is by both `startsit` and `brief`, and will be reused unmodified in Phase 3 for retrospective bench-points-lost tracking.
- Weekly data composition root (`ffai/weekly_context.py`) — resolves my roster and this week's opponent via `ffai/roster.py`, gathers league/rosters/users/players/current-week-state/weekly-projections through `repository.py`'s last-known-good pattern, and aggregates every source's staleness into one list for R1.
- Weekly brief (`ffai/brief.py`, `ffai/cli.py brief --out`) — R6 heartbeat line, R1 staleness banner (states actual data age per stale source, not a placeholder), recommended lineup with changes-vs-currently-set-lineup callouts, injury/depth-chart flags, opponent's currently-set starters for context, `NO PROJECTIONS AVAILABLE` labeling when degraded (R2)
- Start/sit (`ffai/cli.py startsit`) — plain-text rendering of the same optimal-lineup engine
- `refresh` escape hatch (`ffai/cli.py refresh`, R7) — forces a live re-pull of the player dictionary regardless of its normal once-daily cache window, prints the plain-text start/sit summary. Waivers don't exist until Phase 3, so this is the closest present equivalent to "save the bad Tuesday"; its output will extend to include a waiver summary once Phase 3 ships.
- GitHub Actions scheduling (`.github/workflows/weekly_brief.yml`) — Tuesday + Sunday mornings, `workflow_dispatch` for manual runs, commits `brief.md` back to the (private) repo with the default `GITHUB_TOKEN`, no new secrets needed
- ESPN projections fallback fixed to refuse weekly fetches rather than silently return season totals (see §4.2)
- 147 tests passing (`python -m pytest -q`)
- Code pushed to GitHub (commit `2ebd397`); the workflow was manually triggered once via `gh workflow run` (`workflow_dispatch`) and confirmed working end-to-end — checkout, install, generate, and commit-back all succeeded, producing a real `brief.md` commit.
- **Two things still open, not yet verified:**
  1. **The actual cron schedule firing on its own is unconfirmed.** What was tested above is the manual `workflow_dispatch` trigger, which only proves the workflow's mechanics — it does not exercise the `schedule:` cron trigger itself. That can only be observed by waiting for the next real Tuesday or Sunday ~13:00 UTC and checking it fired (`gh run list --workflow=weekly_brief.yml`). Risk is low (GitHub's cron scheduling is a mature, reliable feature) but genuinely unobserved.
  2. **No live verification against a real rostered team.** The sandbox league's rosters are still empty (the Phase 1 mock draft wasn't tied to league rosters — see §6.1). `startsit`/`brief` have been smoke-tested live against the sandbox league's empty-roster/no-matchup-data state (confirms R2 degrades cleanly) but not against real projections or a real optimal-lineup computation. In progress as of this writing: figuring out whether the Sleeper app's commissioner "Edit Roster" tool can add players to this league without running its pending draft first (the league's own draft is real, not a mock, and still shows `status: pre_draft`) — this doesn't require a full draft, just a few players manually added to test with.

### Phase 3 — In season
- Waivers + FAAB (needs a few weeks of real data to be useful anyway)
- Trade evaluation
- Bench-points-lost tracking

---

## 9. Open Questions

| # | Question | Blocking? | Notes |
|---|---|---|---|
| 1 | 2026 league ID | Phase 1 completion | League not yet created. Sleeper mints a **new ID each season**; last year's will not work. Ask commissioner to create early rather than the night before the draft — costs nothing to sit idle and de-risks the draft assistant. |
| 2 | Waiver system: FAAB or rolling priority | No | Read from league settings. Both supported. |
| 3 | Scoring format & roster slots | No | Auto-discovered from league object. |
| 4 | Opponent scouting from league's 2025 season | No | These managers played together last year. `previous_league_id` on the 2026 league links back to it — potentially yields draft tendencies, waiver aggression, FAAB behavior for eleven unfamiliar opponents. Genuinely valuable for a newcomer. Cannot confirm data quality until visible. **Scoping question, not a requirement.** |
| 5 | Keeper/dynasty format? | No (deferred) | Phase 1's draft assistant shipped without keeper/dynasty-specific logic (redraft VORP/tiers only) since the real league's format is still unknown. Changes draft logic substantially if the real league turns out to be keeper/dynasty -- revisit once real league settings are known, before relying on it for the actual draft. |
| 6 | Paid projections (e.g. FantasyPros API) ever worth it? | No | Pricing not verified — do not assume. Not needed for year one. |

### Known non-issues
- User has no Sleeper history (played elsewhere in 2025). No import path exists from other platforms. Irrelevant — the tool works from projections and current roster state, not personal history.
- League not yet created. Does not block Phases 0–1; development proceeds against the sandbox league and mock drafts.

---

## 10. Document Maintenance

The **living copy** of this document lives in the repo as `PRD.md`, version-controlled alongside the code it describes, editable directly by Claude Code.

The **project knowledge copy** is a reference snapshot for orienting a fresh chat. Re-upload only when it has drifted far enough to matter.
