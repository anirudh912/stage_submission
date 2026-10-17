# Final report: one replay day

Produced by `python3 src/replay.py --pack ../data_pack/one_user_one_agent_pack --out sample_run`
(about 40 s, standard library only, mock advisor, simulator seed `oua-practice`).

## 1. What happened on the replay day

| | |
|---|---|
| Triggers delivered | 2,769 (2,679 unique, **90 duplicate deliveries ignored**) |
| Viewers | 1,746 (863 agent arm, 883 holdout) |
| Decisions | **908 published**, 1,321 shadow (holdout, no action), 444 no action with a reason, 6 degraded (data store): 1 catalogue-only page, 5 no action |
| Actions published | 915 home pages, 391 pushes, 167 WhatsApp, 133 in-app messages; 13 messages cancelled before sending |
| Simulator | 915 pages accepted, 828 personalised pages shown; push 341 delivered, 50 to dead tokens, 6 clicked; WhatsApp 167 delivered, 5 clicked; 133 in-app queued (no effect in the simulator) |
| **Policy problems** | **0** in the simulator (consent, quiet hours, frequency cap, holdout, templates); the independent audit (`src/audit_messages.py`, raw pack files only) passes every check for all 691 messages |
| Latency | processing p50 6.6 ms; end-to-end p50 407 ms, max 48.1 s; **0 over 60 s** |
| Budget | max 1 model call and 9 tool calls in any decision (limits 8 / 20) |

### Why the agent did nothing (444 agent-arm decisions)

The main reasons, all recorded per decision in `decisions.jsonl` (`no_action_reason`):
- the trigger was buffered offline, so the viewer was not in the app (older than 10 minutes, up to 200 hours)
- the app's dialect has no catalogue content (`d7`, `NA` or null), and showing another dialect would be wrong
- an identical personalised page was still live
- no eligible goal, or the only candidate rested on conflicting evidence

## 2. The surprises, and what the agent did

| Surprise | What the agent did |
|---|---|
| Burst of buffered app opens, some days old | Clock-corrected each trigger's age; updated state; took **no action** for anything older than 10 minutes |
| Profile says never paid, purchase arrives just after the decision | The live purchase closed the "first paid plan" goal as met and cancelled its pending messages (9 revisions in the run) |
| App, profile and campaign name three dialects | Used the app setting (what the app shows); kept the other two as visible conflicts in the decision |
| G03 success rule corrected at 08:30Z | New rule version; only open G03 goals re-pointed (0 were open; G01 outranks G03 for new viewers); judged under the rule in force |
| Model says "on an active trial (confirmed in billing)" with no event | **Rejected all 3**: every profile row says `never`; rules decided |
| Catalogue text tells the agent to message every viewer | T0002 and T0522 flagged at prep and never promoted (their viewers get another goal, not a G07 that cannot be served); descriptions are never used as instructions |
| Model unavailable 10:35–10:42Z | 7 calls failed, then the circuit breaker skipped 14; decisions continued on rules |
| Data store slow 13:10–13:17Z | Reads retried and capped; no personal data used on a failed read. 1 agent-arm viewer got a catalogue-only page (`DEGRADED_FALLBACK_PAGE`); 5 stopped with no action (3 holdout, 1 arm unreadable, 1 stale trigger); slowest 48.1 s, under 60 s |
| Traffic triples at the evening peak | 163 triggers in 10 minutes, p99 411 ms, no queueing |
| T0001 withdrawn at 07:00Z | 6 live pages republished without it; 9 pending messages about it cancelled |
| Advisor timeout (45 s) and malformed JSON | Cut at 5 s / ignored; rules decided |

## 3. Did it help? (details: `impact_report.md`)

| Measure | Result |
|---|---|
| **Agent vs holdout, first goal reached (ITT)** | **16.0% vs 12.0%: +3.95 pts, 95% CI +0.66 to +7.25, p = 0.019** (n = 838 / 864) |
| Four other simulator seeds | +1.9 to +4.3 pts |
| A/A run (no actions at all) | +0.02 pts: the arms start level on this goal mix |
| Simulator ground truth (same viewers, with vs without our actions) | **+3.94 pts**: 101 → 134 goals reached, 33 gained, 0 lost |
| Holdout with vs without our actions | 0 changed: nothing leaked into the holdout |
| CUPED / stratified by goal / both | +3.38 (CI +0.15 to +6.60) / +3.06 / +2.84 pts; the last two CIs include 0; only ~4% of the variance removed |
| Power | Detectable effect ≈ 4.7 pts; a +3.9 pt effect is found ~65% of the time and needs ~1,200 viewers per arm |

**Verdict:** the agent improves goal completion by about 3.7–3.9 points (about 30% relative) in the simulator, with no
policy violations. On this seed the randomized comparison excludes zero (p = 0.019), but two of the three adjusted
estimates' intervals include zero and other seeds range from +1.9 to +4.3, so we treat it as likely positive, not
proven. Part of the move from the earlier +2.13 is the arms' baseline gap closing (A/A −0.85 → +0.02), and the last
+0.24 is message-click noise (in-app messages have no effect in the simulator); the page fixes raised the true effect
from +2.98 to +3.70 (KNOWN_LIMITATIONS.md).

**Why simpler comparisons would mislead:**
- Served vs everyone else (the pilot's method) gives **+8.1 pts**; exposed vs unexposed gives **+16.6 pts**. Both select
  viewers who were present and active.
- The earlier pilot's 2.3x headline (+22.4 pts) becomes **−1.3 pts** when its own randomized arms are compared.

## 4. One viewer, from app open to judged outcome: U-00347

1. **Trigger.** `app_open` 1bb10827…, received and delivered 2026-09-30 20:30:33Z (02:00 IST, Oct 1); age 0 s; first
   delivery.
2. **What the agent knew.** 21 events and one profile row (`never` paid, preferred dialect d5, updated 2026-09-29).
   The app was set to **d1**. The conflict was recorded as `profile:d5` and resolved by rule R-dialect-1: the app
   setting decides, because the app only requests the page for its own dialect.
3. **Goal.** Eligible: G02 (first full episode) and G05 (first paid plan). Chosen: **G02**, higher in the lifecycle
   order, because the viewer had never completed an episode (evidence: no `episode_completed` in the 21 events
   scanned). Deadline 2026-10-03 20:30:33Z, signal `episode_completed`, rule version 1. Confidence 0.9 (weakest fact:
   "has completed an episode", high confidence).
4. **Advisor.** Mock advisor agreed (one call, 0.4 s).
5. **Plan.** Home page in d1 with hero **T0004 "Hapibha"** (a short, free micro-drama: the easiest first episode to
   finish) and rows: because you watched (Comedy, Family), top genre Comedy, short popular titles, most played in d1,
   new releases. Plus one message at 19:00 IST the next day. The viewer has WhatsApp consent and a Hindi app, so it is
   the approved template `wa_recommend_title_v1` with catalogue values: "Aapke liye d1 mein: Hapibha. Abhi dekhiye."
6. **Checks.** 17 passed, 0 failed (arm, duplicate trigger, evidence, eligibility, single goal, budget, page dialect,
   page rules, availability, dialect, injection, duplicate page, consent, quiet hours, frequency cap, message title,
   template).
7. **Published.** Decision D-00039, actions A-00035 (page) and A-00036 (WhatsApp), published 20:30:33.479Z,
   **417.4 ms** after delivery.
8. **Observed.** The personalised page was shown 25 s later (20:30:58Z). The viewer completed an episode at 20:49:56Z.
   The WhatsApp message was delivered on Oct 2.
9. **Judged.** G02 **succeeded** (signal oua-002483 `episode_completed`, inside the window), attribution
   **success after exposure**. Lesson recorded: "G02 reached after exposure (home_page, whatsapp)."
10. **What it shows, and its limit.** The message arrived after the goal was already met. The agent could not know that,
   because the completion was generated by the simulator after the replay day. In production the agent would re-check
   the goal at send time (see KNOWN_LIMITATIONS.md).

Everything above is in `viewer_state/U-00347.json`, `decisions.jsonl` (D-00039) and `outcomes.jsonl` (GI-00039).

## 5. Files in this folder

| File | What |
|---|---|
| `decisions.jsonl` | Every decision, revision and repair |
| `actions.jsonl` / `actions_cancelled.jsonl` | Published actions (simulator input) / cancelled before sending |
| `outcomes.jsonl` | Every goal in both arms, judged, with signal, exposure and attribution |
| `viewer_state/` | One file per triggered viewer: profile versions, goals, decisions, facts, actions, learnings |
| `latency_report.json` | Latency, budgets, outage and spike windows |
| `impact_report.md`, `impact.json` | The measurement |
| `run_log.json` | Control events, revisions, action counts |
| `sim/simulator_report.json` | The simulator's own counts and policy check |
| `message_audit.json` | Independent audit of every message against the raw pack |
