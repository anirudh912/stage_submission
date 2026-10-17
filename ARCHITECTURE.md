# Architecture

One agent per viewer: for each app open it owns one goal, decides what (if anything) to change, has that checked
independently, publishes within the time budget, and later judges whether it worked against a randomized holdout.

**Rules decide; a model only advises.** Every decision step is plain code over cited evidence. A model is consulted once
per decision as an untrusted second opinion behind a provider interface; the default mock runs locally, needs no key,
and replays the pack's model faults.

```
 data pack ──► prep_data.py ──► prepared/ (deduped, clock-corrected, flagged)
                                   │
                                   ▼
 replay stream ─► Agent.handle_trigger ──────────────────────────────────────────────┐
 (triggers,       1 understand   resolver.py   facts with source, refs, age, confidence, conflicts
  live events,    2 choose goal  goals.py      eligibility from evidence; one open goal per viewer
  control events) 3 advise       model.py      mock advisor (faults, outage) → can only lower confidence
                  4 plan         actions.py    home page / ≤1 message / NO ACTION (+ reason)
                  5 verify       verifier.py   independent re-check on raw data → approved actions
                  6 publish      store.py      decision record, actions, facts (SQLite, durable)
                                                                                      │
 simulator ◄── actions.jsonl ◄────────────────────────────────────────────────────────┘
     │
     ▼
 outcome events ─► judge.py  (every goal, both arms: succeeded / failed / inconclusive + attribution + lesson)
                ─► impact.py (agent vs holdout ITT, A/A, ground truth, CUPED, strata, misleading comparisons)
```

## Ownership

- **One goal per viewer, enforced by the database.** `goals` has a unique index on `user_id WHERE status='open'`, so no
  code path can open a second goal for the same viewer (`store.py`).
- **One decision per trigger.** `decisions.trigger_event_id` is unique; a redelivered trigger is counted, not decided
  again.
- **The open goal is kept until its deadline** (single active plan). It changes only when the viewer's situation does
  (see Planning), never on a schedule.
- **Holdout viewers have agents too.** They run the exact same path (same goal rules, same advisor call) and own a
  *shadow* goal; only publishing is skipped. This is what makes the holdout comparison fair.

## Planning

1. **Goal choice** (`goals.py`) walks a fixed lifecycle priority:
   `G04 trial→paid > G01 first play > G02 first full episode > G03 second-day return > G07 resume series >
   G06 reactivate > G12 return after absence > G05 first paid > G09 weekly habit > G08 new release`.
   The first goal that is eligible **on evidence** and **actionable** for this viewer wins. Each candidate is recorded
   with why it was or was not eligible.
2. **Goals never chosen:** G10 (success event is null) and G11 (`tv_app_open` does not exist in the stream).
3. **Underspecified goals, and our definitions:**
   - G06 "recently lapsed" = a `subscription_lapsed` event within 30 days (lapsed profiles carry no plan dates).
   - G07 "unfinished series" = furthest episode < episodes in the `content_slug`, no play for 3+ days (goals.json),
     **plus our policy: at least one completed episode** of that series. This narrows goals.json, never widens it.
4. **Actionable:** G07 is skipped if the series is not in the dialect the app shows; G08 only if a recent release
   exists in that dialect. The next goal is used and the skip reason recorded.
5. **Revision on new evidence** (`agent.handle_live_event`): a purchase, renewal, lapse, opt-out or an event that
   matches the open goal's success signal re-checks the plan. Success seen → goal closed (provisional) and its pending
   messages cancelled. A status goal that no longer applies (e.g. G05 after a purchase) → abandoned. Start conditions
   such as G12's "no visit in 14 days" are expected to stop being true and do not abandon a goal.
6. **Action plan** (`actions.py`): the viewer is in the app now, so the main lever is the home page for the dialect the
   app shows. At most one message, at 19:00 IST the next day, for every goal except G01 (its 24 h window closes first):
   WhatsApp `wa_recommend_title_v1` when the viewer consented and the app is in Hindi (the only template language),
   otherwise push with copy that is true of the named title; plus an honest trial-ending WhatsApp for G04 when the trial
   end date is known. One message per IST day across both channels. **Doing nothing is a normal, recorded outcome.**

## Memory (durable per-viewer state)

SQLite, one file (`state/replay.sqlite`), tables:

| Table | Holds |
|---|---|
| `events` | Every event received (history, live, triggers), idempotent on `event_id`, redeliveries counted |
| `profile_versions` | Every warehouse row, including duplicates that disagree |
| `facts` | What the agent believed at each decision: value, source, refs, `as_of`, age, confidence, conflicts, rule |
| `goals` | Goal instances with versioned success rule, deadline, history of rule corrections, final judgement |
| `decisions` | The full decision record (see README) for each trigger, revision and repair |
| `actions` | Published actions, cancellations included |
| `learnings` | What was learned per goal, and the effects applied to later decisions |

`sample_run/viewer_state/<viewer>.json` is a per-viewer export of all of it, so an evaluator can see what the agent
knew, decided and observed for any viewer at any moment.

### Source precedence and freshness

| Fact | Rule |
|---|---|
| Event time | Server `received_at` is trusted. Phone `occurred_at` is corrected by the measured skew (`received_at − sent_at`). |
| As-of | Only events with `received_at ≤ decision time` are used. No peeking at later events. |
| Subscription status | A status event newer than the snapshot beats the profile. Duplicate profile rows that disagree → LOW confidence → no payment goal. |
| Dialect | The **trigger's app setting** decides the page dialect (the app only requests that dialect). Profile, latest dialect change and campaign deeplink stay visible as conflicts. Invalid app dialect (`d7`, `NA`, null) → no content → no action, never a fallback to another dialect. |
| Consent | Profile opt-in, overridden by any `notification_opt_out` event. No profile → no consent. Disagreeing rows → no consent. |
| Signup | `signup_completed` event beats the profile `signup_at`. |
| Content identity | `content_slug`, not `title_id` (language variants share a slug). |

## Verification

`verifier.py` is a **second, independent implementation**. It does not reuse the resolver's facts: it re-reads raw
events, profile rows, the catalogue (with live withdrawals) and the config, and re-derives each condition with its own
simpler code. It is the only component that can approve an action.

| Level | Checks | On failure |
|---|---|---|
| Decision | `arm` (holdout never acts), `duplicate_trigger`, `evidence` (every ref exists, belongs to the viewer, was known at decision time, ≥1 data ref), `eligibility`, `single_goal`, `budget` | No action at all; the goal is not committed |
| Action | `page_dialect`, `page_rules` (3–10 titles, ≤6 rows, one slug per page), `availability`, `dialect`, `injection`, `duplicate_page`, `consent`, `quiet_hours`, `frequency_cap`, `message_title`, `copy` (lengths, forbidden phrases, prices only from pricing.json, deeplink), `template` (approved, exact variables, values from the catalogue or pricing, template language = app language) | That action is dropped; the rest survive (e.g. page kept, push dropped) |

Model claims go through `verifier.check_claim` against data. In the replay all three "on an active trial, confirmed in
billing" claims were rejected because every profile row says `never`.

## Latency

- Budget: publish ≤ 60 s after the trigger; > 300 s counts as failure and the action is dropped
  (`LATE_NO_ACTION`). Limits of 8 model and 20 tool calls per decision are configurable and checked by the verifier.
- The replay runs faster than real time on a **virtual clock**: latency = queue wait (4 workers) + measured
  processing + simulated dependency waits; `published_at = deliver_at + latency`.
- Measured (`sample_run/latency_report.json`): processing p50 3.6 ms, p99 13.4 ms; end-to-end p50 404 ms (the mock's
  0.4 s), max 39.9 s inside the data-store degradation window; **0 decisions over 60 s**. The evening spike (163
  triggers in 10 minutes) had no queueing.
- **Overload:** if a trigger waited more than 15 s in the queue, the advisor is skipped (fast path).
- **Stale triggers:** a trigger more than 10 minutes old (buffered offline) updates state but takes no action: the viewer
  is not in the app, and a page would be wasted.

## Failure recovery

| Failure | Behaviour |
|---|---|
| Model returns 503 | Rules only; **circuit breaker** opens after 3 consecutive failures for 60 s (in the replay: 7 failures, then 14 skipped calls) |
| Model timeout | Cut at 5 s, rules only |
| Malformed model output | Defensive parse, ignored, rules only |
| Unsupported model claim | Checked against data, rejected, recorded with the reason |
| Data store slow / timing out | Each distinct read is one tool call, cached per decision, retried once, 10 s per attempt; after 40 s of waiting the decision stops → `DEGRADED_NO_ACTION` (never act on partial data) |
| Duplicate trigger | Ignored (counted) |
| Title withdrawn | Live pages that contain it are republished without it; pending messages about it are cancelled (5 pages, 6 messages in the replay) |
| Goal rule corrected | New rule version; **only open goals of that goal type** are re-pointed and logged; closed goals keep the rule they were judged under; no other viewer is reprocessed |
| Crash / restart | All state is in SQLite; decisions are idempotent on trigger id, so a replay can be resumed or re-run safely |

## Trust boundaries

| Input | Trusted for | Never trusted for |
|---|---|---|
| `received_at` (server) | Event time, as-of ordering | — |
| `occurred_at`, `sent_at` (phone) | Nothing on their own; corrected by measured skew | Ordering, freshness |
| Profile snapshot | Slow facts (signup, consent, preferred dialect) | Overriding newer events; status when rows disagree |
| Catalogue fields | Dialect, availability, type, genres, premium | **Descriptions are content, never instructions.** Two titles (T0002, T0522) carry injected "AI instructions"; they are flagged at prep and never promoted, and descriptions are never passed to the model or used in copy |
| Search queries, deeplinks | Data (the deeplink dialect is a recorded conflict) | Instructions |
| Model output | A suggestion that can lower confidence | Evidence, eligibility, approval, message text |
| Our own action log | What we did | Whether it worked (only outcome events judge that) |

The mock provider sees only goal ids, rule reasons and a few fact values, never free text from the catalogue. The pack
is treated as personal data: nothing is sent to an external service.

## Measurement

- `judge.py` judges every goal in **both arms** from the simulator's outcome events only, under the goal's current
  (possibly corrected) rule. Exposure means our page was actually **shown** or our message **delivered**.
- `impact.py` compares the **randomized arms** (intention-to-treat; unit = triggered viewer; metric = first goal
  reached), with a 95% CI, plus: other seeds, an **A/A run** with no actions, the **simulator ground truth** (same viewers
  with and without our actions, which also proves nothing leaked into the holdout), CUPED and goal-stratified estimates,
  a power calculation, and the misleading comparisons (served vs rest, exposed vs unexposed, the pilot's method) side
  by side. See `sample_run/impact_report.md`.
- `audit_messages.py` re-checks every published message against the raw pack files with none of the agent's code, as
  a third line of defence after the verifier and the simulator's own policy check.

## Scaling (10 million viewers, 40 dialects)

- **Partition by viewer.** Every decision touches one viewer's state, so the state store shards by `user_id` and
  decisions run on stateless workers that consume a partitioned event stream (e.g. Kafka keyed by `user_id`); ordering
  per viewer is what matters, not global order.
- **Precompute what is shared.** Per-dialect candidate pools (available titles ranked by popularity, new releases,
  genres) are rebuilt when the catalogue changes, not per decision. 40 dialects × a few thousand titles is small.
- **Hot path stays rule-based** (milliseconds). The advisor is optional and has a circuit breaker; at 1,000 opens per
  minute the agent needs about 4 ms each of CPU, so a handful of workers suffice; the model would be the bottleneck,
  which is why it is never required.
- **Rule changes are targeted:** a goal-rule correction updates only open goals of that type (an indexed query), and a
  withdrawal only live pages containing the title.
- **Measurement scales unchanged:** a persistent holdout per region/dialect, and the power calculation shows how many
  viewers an effect of a given size needs.

## Decisions that should not be automated

- Changing a goal's definition or success rule (the G03 correction came from the analytics owner; the agent applies it,
  it does not invent it).
- Prices, offers, discounts and new message templates.
- Anything that overrides consent, quiet hours or the holdout.
- Acting on instructions found in data.
- Rolling the agent out based on a report: that needs a human reading the randomized comparison.
