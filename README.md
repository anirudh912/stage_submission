# One User, One Agent

A per-viewer lifecycle agent for the OTT dialect platform. For each viewer who opens the app it chooses **one
measurable goal**, decides what to change (a home page, at most one message, or **nothing**) from evidence it can cite,
has that checked by an **independent verifier**, publishes within the time budget, and afterwards **judges whether it
helped against the randomized holdout**.

Decisions are made by **rules over evidence**. A model is only an untrusted advisor behind a provider interface; the
default **mock provider** runs locally, needs no API key, reproduces the pack's model faults, and sends no data anywhere.

## Results on the supplied replay day

| | |
|---|---|
| Decisions | 2,679 (90 duplicate deliveries ignored); 908 published, 1 catalogue-only fallback page, 444 no action with a reason, 1,321 holdout shadow |
| Policy problems | **0** in the simulator; the independent audit passes every check for all 691 messages (391 push, 167 WhatsApp, 133 in-app) |
| Latency | p50 407 ms, max 48.1 s, **0 over 60 s** (processing p50 6.6 ms) |
| Injected faults | all 3 unsupported "on trial" model claims rejected; outage, timeout, malformed output and slow data store handled safely |
| **Impact, agent vs holdout** | **+3.95 pts** goals reached (16.0% vs 12.0%), 95% CI +0.66 to +7.25, p = 0.019 |
| Simulator ground truth | +3.94 pts (same viewers with vs without our actions); 0 holdout viewers affected |

The interval excludes zero on this seed, but only just: other seeds give +1.9 to +4.3, two of the three adjusted estimators'
intervals include zero, and the test has about 65% power for an effect this size. `sample_run/impact_report.md` explains this, and
why the simpler comparisons (served vs rest: +8.1; the pilot's +22.4) mislead.

## Requirements

- Python 3.9+ (standard library only, nothing to install)
- The data pack, extracted next to this repo:

```
Stage Assignment/
├── data_pack/one_user_one_agent_pack/   # the supplied pack (not committed: personal data)
└── stage_submission/                    # this repo
```

## Run

```bash
cd stage_submission
python3 src/replay.py --pack ../data_pack/one_user_one_agent_pack --out sample_run
```

One command, about 40 seconds. It prepares the data if `prepared/` is missing, replays the trigger day (triggers, live
events and control events in delivery order, with the injected faults), runs the supplied simulator on the published
actions, judges every goal in both arms, and measures impact.

| Output in `sample_run/` | What |
|---|---|
| `decisions.jsonl` | Every decision, revision and repair: trigger, goal and deadline, dialect and conflicts, content, confidence with reason, evidence with freshness, assumptions, checks, advisor outcome, budget, latency, status, reason for no action |
| `actions.jsonl` | Published actions (the simulator's input); `actions_cancelled.jsonl` holds withdrawn ones |
| `outcomes.jsonl` | Every goal judged succeeded / failed / inconclusive, with signal, exposure and attribution |
| `viewer_state/<viewer>.json` | Per-viewer state: profile versions, goals, decisions, facts, actions, learnings |
| `latency_report.json` | Latency percentiles, budget use, outage and spike windows |
| `impact_report.md`, `impact.json` | Agent vs holdout, seeds, A/A, ground truth, CUPED, strata, power, misleading comparisons, pilot re-check |
| `final_report.md` | Summary of the run, the surprises, and one viewer walked from app open to judged outcome |
| `run_log.json`, `sim/simulator_report.json` | Control-event handling; the simulator's own counts |

Independent policy audit of every published message, against the raw pack files only (imports none of the agent's
code; exits non-zero on any violation; writes `sample_run/message_audit.json`):

```bash
python3 src/audit_messages.py ../data_pack/one_user_one_agent_pack sample_run
```

Options: `--seeds` (simulator seeds, first is the main run), `--workers` (virtual decision workers, default 4),
`--max-model-calls` (8), `--max-tool-calls` (20), `--no-simulate` (stop after the replay), `--state`, `--prepared`.
To use another replay day or seed, point `--pack` at it or pass `--seeds`.

## Tests

```bash
python3 -m unittest discover -s tests -v      # 90 tests, about 0.6 s
```

The four scenarios the brief requires (late or duplicate trigger, conflicting source, tool failure, corrected goal
rule) are in `tests/test_required_scenarios.py`; `tests/README.md` maps every test file to what it covers.

## Layout

| Path | What |
|---|---|
| `src/prep_data.py` | Dedupes events, corrects phone clocks, flags late events, invalid dialects and injected text; title popularity |
| `src/agent/store.py` | Durable per-viewer state (SQLite); one open goal per viewer and one decision per trigger, enforced by the database |
| `src/agent/resolver.py` | Facts with source, freshness, confidence, conflicts and the precedence rule that decided |
| `src/agent/goals.py` | Eligibility from evidence, goal choice, single active plan, success detection, rule corrections |
| `src/agent/actions.py` | Home page, at most one message, or a recorded "do nothing" |
| `src/agent/verifier.py` | Independent checks on raw data; the only component that can approve an action |
| `src/agent/model.py` | Provider interface, mock advisor with fault injection, defensive parsing, circuit breaker |
| `src/agent/deps.py` | Data-store wrapper: tool-call counting, per-decision cache, injected slowness and timeouts |
| `src/agent/agent.py` | The per-viewer loop, live-event revisions, withdrawal repair, rule corrections |
| `src/agent/judge.py` | Goal judgement from outcome events, attribution, lessons and their feedback into decisions |
| `src/agent/impact.py` | Holdout comparison and its checks |
| `src/replay.py` | Replay harness and CLI |
| `src/audit_messages.py` | Independent post-run audit of every message against the raw pack (18 checks per push, 16 per WhatsApp) |
| `tests/` | Unit and scenario tests |
| `sample_run/` | Output of the command above |

## Documents

- [ARCHITECTURE.md](ARCHITECTURE.md): ownership, planning, memory, verification, latency, failure recovery, trust
  boundaries, scaling, and decisions that should not be automated
- [AI_COLLABORATION.md](AI_COLLABORATION.md): how AI tools were used, how their output was checked, what was rejected
- [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md): what is intentionally incomplete
- [sample_run/final_report.md](sample_run/final_report.md) and [sample_run/impact_report.md](sample_run/impact_report.md)

## Privacy

The pack is treated as personal data even though it is synthetic. Nothing is uploaded anywhere: the advisor is a local
mock, and `sample_run/` contains derived per-viewer data, so keep this repository private.
