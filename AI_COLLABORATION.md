# AI collaboration log

## Tools

- **Claude Code** (Anthropic, Claude Opus) in the terminal: read the brief and the pack, profiled the data, wrote the
  code, tests and documents, ran every step, and reported results.
- No AI model is used at runtime. The agent's "model" is a local mock advisor (see ARCHITECTURE.md).

## How the work was split

I directed the work step by step and approved each phase before the next one started:

| Phase | What I asked for | What Claude produced |
|---|---|---|
| Orientation | Summary of the brief and the pack; a plan; prerequisites | Data profile, list of traps, 12-step plan |
| Design choice | Whether the agent can work without a model | Options; I chose **rules decide + mock advisor** |
| Steps 1–4 | Data prep, state store, fact resolver, goal picker | `prep_data.py`, `store.py`, `resolver.py`, `goals.py`, 19 tests |
| Steps 5–6 | Action builder, independent verifier | `actions.py`, `verifier.py`, 22 tests |
| Steps 7–8 | Mock model, replay runner | `model.py`, `deps.py`, `agent.py`, `replay.py`, 13 tests |
| Steps 9–10 | Judge, impact report | `judge.py`, `impact.py`, 12 tests |
| Tuning round | Options to raise impact | Options A1, A2, B1, B2 and a list of rejected tricks; I chose A1 + A2 + B1, not B2 |
| Review round | Are we picking the wrong goals? Is messaging right? | Goal-fit analysis (reported, not acted on); an independent message audit; WhatsApp channel turned on at my request |
| Steps 11–12 | Required tests, documents, sample run | `test_required_scenarios.py`, these documents |

I also kept a private running notes file with every data rule, decision and result, so I could check the work against
it at each step.

## How the output was checked

- **Every step was run, not just written.** After each phase the full replay day went through the real simulator, and
  `simulator_report.json` was checked for policy problems (0 in every run) and rejected pages.
- **Unit tests for every component** (90 at the end), including the four scenarios the brief requires.
- **Claude read the simulator's source before designing actions**, instead of guessing how pages and messages are
  scored (page TTL, the 3–40 s window before the home screen, what counts as a visit).
- **The impact number was checked against things that cannot be fooled:** an A/A run with no actions (should be about
  zero), and a same-seed ground truth (same viewers with and without actions), which also showed that no action leaked
  into the holdout.
- **Spot checks of real outputs:** sample pages, pushes and decision records were read by eye, not only counted.
- **An independent message audit** (`src/audit_messages.py`) re-checks every message against the raw pack files with
  none of the agent's code, so it can catch what the agent's own verifier would miss.

## What was caught, corrected or rejected

### Bugs in Claude's code that the checks caught

- **38 false "unsupported claim" rejections.** The advisor was not told the viewer already had an open goal, so it
  suggested a different goal, and G07 claims were checked without the target series. Found by reading the advisor
  outcome counts (41 rejections when only 3 faults were planted); fixed, and the count became exactly 3.
- **A decision record missing fields** on the degraded-data-store path (crashed the report). Fixed so every record has
  the same fields.
- **A "return after absence" goal abandoned when the viewer returned.** Its start condition is supposed to stop being
  true; only status-based goals may be abandoned. Found in the replay's revision log; fixed and tested.
- **Dishonest push copy.** The first version said "a new story" for any title. Replaced with copy tied to a property of
  the named title (short episode, new release, premium, unfinished series).
- **Page-builder gaps, found by scoring our own pages with the simulator's formula.** d3 pages averaged 2.1 rows
  (the first row took most of the 13 titles); on 100 pages the series hero left the `next_episode` row one title short
  and the platform dropped it; 61 pages had no hero because their G07 series was flagged for injected text and could
  not be shown. Fixed (shorter rows in a thin catalogue, series leads the row, fallback hero, G07 not chosen for a
  series that cannot be promoted), plus a catalogue-only page when the data store fails. True effect +2.98 → +3.70
  pts; the headline's larger move is partly a changed baseline gap, which is reported.
- **In-app messages, added on my request for viewers no other channel reaches.** The independent audit caught the
  first version queuing them before their publish time; fixed. The true effect then moved +3.70 → +3.94, although the
  simulator ignores in-app messages: Claude showed the simulator's output is byte-identical with and without them, so
  the move is click randomness from shifted action ids, and it is reported as noise.
- **A push sent after the goal was already met.** Found while preparing the walkthrough. Partly fixed (goals met
  during the replay now cancel their pending push); the rest is a documented limitation.

### Tests that were wrong, not the code

- A circuit-breaker test spaced calls exactly one cooldown apart, so the breaker had already reset.
- A lessons test expected "avoid this goal" for a viewer who was never actually exposed. The judge was right.

Both were fixed in the test, after checking that the code's behaviour was the intended one.

### Where I overruled or limited the suggestions

- **G07 (resume series):** Claude flagged that it was 48% of goals with no measurable effect and offered three
  options. I chose to require at least one completed episode.
- **Goal fit:** Claude's analysis showed that for 170 active subscribers our actions move a different goal (new
  release) more than the one chosen (resume series), and traced it to how the simulator picks titles. I chose to report
  the analysis instead of re-ranking goals.
- **WhatsApp:** I chose to turn on the recommendation template despite its poor wording (see below).
- **B2 (lower G07's priority): rejected.** It would raise the score on this seed and replay day without helping viewers,
  and risked overfitting to data the evaluators will not use.

### Where Claude corrected itself

- Claude first said the WhatsApp recommendation template could not be used honestly, because the pack has dialect
  codes, not names. On review it corrected this: the policy requires variables to come from the catalogue, and the
  code *is* the catalogue value, so the message follows the rules but reads poorly. I chose to use it for viewers with
  WhatsApp consent whose app is in Hindi; the effect on impact was neutral and is reported as such.

### Where Claude pushed back on me

- I asked to lower G07's idle threshold from 3 days to 2. Claude did not apply it, because `goals.json` defines the
  goal as "3 or more days", so the agent would assign goals to ineligible viewers (and the verifier would reject them).
  I kept the official 3 days.

### Ideas rejected because they would game the simulator

| Idea | Why rejected |
|---|---|
| Pad the `next_episode` row with titles the viewer never started (the simulator rewards any in-progress title in that row) | The row label would be false |
| Publish pages for predicted future visits | Not a response to a trigger; violates `published_at = deliver_at + processing` |
| Pick easier goals to raise the success rate | Viewers are not helped more; only the metric moves |
| `wa_new_episode_v2` for resuming a series | Claims a new episode arrived when none did |
| A same-day push | The viewer is already in the app; G03 needs a later day anyway |

## Candidate's notes

_To be completed by me: what I learned, what I would do differently, and which AI suggestion I most disagreed with._
