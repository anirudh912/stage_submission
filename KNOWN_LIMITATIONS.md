# Known limitations

What is intentionally incomplete or simplified, and why.

## Measurement

- **The effect is significant on this seed, but not robustly.** Agent vs holdout is +3.95 pts (95% CI +0.66 to
  +7.25, p = 0.019), close to the simulator's true effect (+3.94). Other seeds give +1.9 to +4.3; CUPED gives +3.39
  (CI +0.17 to +6.61), but the stratified estimates (+3.06, +2.86) have intervals that include zero. With about 850
  viewers per arm the test reliably detects only about 4.7 pts, so an effect of this size is found about 65% of the
  time (it would need about 1,200 viewers per arm). CUPED and goal stratification remove only about 5% of the variance here, because
  pre-replay activity barely predicts goal success in this simulator.
- **Part of the jump from the earlier +2.13 is chance, not the agent.** The page fixes raised the true effect from
  +2.98 to +3.45 pts; skipping G07 when its series cannot be promoted raised it to +3.70. That second change also
  moved about 50 viewers in both arms to other goals, which changed the arms' baseline gap (A/A −0.85 → +0.02). Most
  of the headline's move comes from that gap, not from a bigger effect. Adding in-app messages then moved the truth
  to +3.94; that is click noise (see below), not an effect.
- **The ground truth is a simulator-only check.** Comparing the same viewers with and without our actions is possible
  only because the simulator is deterministic per seed. A real deployment has only the randomized comparison.
- **One goal per viewer is measured** (the first). Later goals in the same window are tracked and judged but not
  pooled into the headline, to keep one unit per viewer.
- **Attribution is descriptive.** "Success after exposure" per viewer is not causal; only the arm comparison is.

## Decisions

- **G07 "resume series" has a small effect.** Returning to the *same* series within 72 hours is rare in the simulator
  (true lift 2.7% → 4.7%), and only a push click delivers that series. We tightened its definition (at least one completed
  episode), which moved 154 viewers to better-suited goals, but kept it in the priority list and did not lower its
  rank, to avoid tuning to one seed and one replay day.
- **Goal fit (impact report, section 1):** for 132 active subscribers the agent picks G07 by priority, yet the
  simulator shows our actions move their G08 (new release) outcome more (+6.8 vs +1.9 pts). The cause is the
  simulator: a page raises the chance of playing *any* title, and the title is drawn from the dialect's catalogue, so a
  goal tied to one specific series can only be moved by a push click. A hero for the exact series a viewer left is the
  right real-world choice, so the priority was kept and the finding reported instead.
- **The goal priority order is a fixed, hand-written lifecycle order**, not learned. Lessons only skip a goal that
  failed after exposure (for 7 days).
- **Title ranking is popularity within the dialect** plus the viewer's top genres. No collaborative filtering or
  model-based ranking.
- **Push copy uses a small set of fixed sentences** per goal, each true for the title it names. No generated copy.
- **WhatsApp `wa_recommend_title_v1` shows a dialect code, not a name.** The template's `dialect_name` must come from
  the catalogue, and the catalogue only has codes, so viewers read "Aapke liye d3 mein: <title>". It follows the
  policy and is not false, but it reads like a system code; a real team would add dialect names to the catalogue. It is
  used for viewers with WhatsApp consent whose app is in Hindi (167 messages), replacing the push. In this run it
  reached 24 viewers push could not, but made no measurable difference to the effect (+3.10 → +2.98 pts, one viewer).
- **The trial-ending WhatsApp never fired:** the one trial viewer served has no trial end date in the data, so "your
  trial ends soon" could not be said honestly. `wa_new_episode_v2` is never used: it would claim a new episode arrived.
- **In-app messages have no effect in the simulator.** They are used only for viewers no other channel can reach
  (no push/WhatsApp consent or no profile, or a goal like G01 whose window closes before the next evening slot): 133
  in this run. The simulator only counts them as queued; its outcome events are byte-identical with and without them.
  Adding them still moved the true effect from +3.70 to +3.94 pts, because new actions shift the action ids and the
  simulator seeds message clicks by action id (push clicks 1 → 6, WhatsApp 8 → 5). That move is noise, not impact.
- **When an in-app message is shown is inferred**: the agent treats it as shown at the viewer's next app open and
  stops cancelling it after that. The platform does not report display.
- **G01 gets no push**, because its 24-hour window usually closes before the next-evening slot; it gets an in-app
  message instead.

## Real-time behaviour

- **Messages are scheduled for the next evening and cannot be cancelled by events the simulator generates later.**
  The simulator runs after the replay, so a viewer who reaches the goal within minutes still gets the message the next day
  (example: U-00347). The agent cancels pending messages when the goal is met *during* the replay; in production it
  would also re-check the goal at send time.
- **Pages expire after 2 hours** and are only republished on the next trigger. The agent does not schedule pages for
  predicted future visits (that would not respond to a trigger).
- **Concurrency is simulated** with a 4-worker queue on a virtual clock. State changes are applied in delivery order,
  even when a decision "starts" a little later in virtual time.
- **The data-store degradation applies to reads only.** Writes go to the agent's own journal and are assumed healthy.
- **The degraded-store fallback page is generic.** With no viewer data it can only show the most-played titles of the
  app's dialect. It is not shown when the arm could not be read (holdout safety comes first). It covered 1 viewer in
  this run.
- **Goals whose deadline passes during the replay** are closed at the viewer's next decision (provisional); the judge's
  final verdict after the simulator replaces it.

## Data handling

- **Profile conflicts that events cannot settle** (22 viewers whose duplicate rows disagree on payment) are not
  resolved; payment goals are simply not chosen for them.
- **Viewers with an app dialect that has no catalogue content** (`d7`, `NA`, null) get no action; the agent does not
  guess another dialect.
- **Clock correction assumes the phone sends promptly** after it records `sent_at`. A phone whose clock is wrong *and*
  that buffers for a long time can still be mis-timed by the buffering gap.
- **Catalogue injection detection is a keyword pattern** on descriptions. It flags the two planted titles; the real
  protection is that descriptions are never used as instructions, copy or model input.

## Engineering

- **Standard library only, single process, SQLite.** No service, API or UI; the CLI is the interface.
- **The model provider is a mock.** The interface (`ModelProvider.advise_goal`) is where a real provider would plug
  in; none is wired up, so no viewer data leaves the machine.
- **The middle commits in git history are not runnable on their own**: several files were edited across phases and
  committed grouped by file. The latest commit runs and passes all tests.
- **`sample_run/` is about 40 MB** (one JSON file per triggered viewer). The simulator's raw event dump is regenerated
  rather than committed.
