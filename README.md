# One User, One Agent

A per-viewer lifecycle agent for the OTT dialect platform take-home. For each viewer it chooses one measurable goal,
decides what to change (home page, one message, or nothing) from evidence it can cite, checks that decision
independently, acts within a time budget, and later judges whether it helped, measured against a randomized holdout.

Decisions are made by **rules over evidence**. A language model is only an optional advisor behind a provider
interface; the default **mock provider** needs no API key and never sends data anywhere.

## Requirements

- Python 3.9+ (standard library only, nothing to install)
- The data pack, extracted next to this repo:

```
Stage Assignment/
├── data_pack/one_user_one_agent_pack/   # the supplied pack (not committed)
└── stage_submission/                    # this repo
```

## Setup

```bash
cd stage_submission
# 1. clean and index the pack -> prepared/ (about 5 s)
python3 src/prep_data.py --pack ../data_pack/one_user_one_agent_pack --out prepared
# 2. run the tests
python3 -m unittest discover -s tests -v
```

## Replay

One command replays the whole trigger day (it runs the data prep first if `prepared/` is missing):

```bash
python3 src/replay.py --pack ../data_pack/one_user_one_agent_pack --out sample_run
```

It takes about 35 seconds. It replays the day, runs the supplied simulator on the published actions, judges every
goal in both arms, and measures impact against the holdout. It writes to `sample_run/`:

| File | What |
|---|---|
| `decisions.jsonl` | Every decision, revision and repair, with goal, evidence, checks, confidence, latency, status |
| `actions.jsonl` | Published actions (the simulator's input); `actions_cancelled.jsonl` holds withdrawn ones |
| `outcomes.jsonl` | Every goal (both arms) judged succeeded / failed / inconclusive, with signal and attribution |
| `viewer_state/<viewer>.json` | Per-viewer state: profile versions, goals, decisions, facts, actions, learnings |
| `latency_report.json` | Latency percentiles, budget use, outage and spike windows |
| `impact_report.md`, `impact.json` | Agent vs holdout, seeds, A/A check, simulator ground truth, misleading comparisons, pilot re-check |
| `run_log.json`, `sim/` | Control-event handling and the simulator's own output |

Options: `--workers` (virtual decision workers, default 4), `--max-model-calls` (8), `--max-tool-calls` (20),
`--seeds` (simulator seeds, first is the main run), `--no-simulate` (stop after the replay).

The model is a local **mock advisor**. It reproduces `replay/model_faults.jsonl` at the named triggers, and the
harness injects the provider outage and the data-store degradation from `control_events.jsonl`.

## Layout

| Path | What |
|---|---|
| `src/prep_data.py` | Dedupes events, corrects phone clocks, flags late events, invalid dialects and injected text |
| `src/agent/store.py` | Durable per-viewer state (SQLite): events, profile versions, facts, goals, decisions, actions, learnings |
| `src/agent/resolver.py` | Turns events and profile rows into facts with source, freshness, confidence and conflicts |
| `src/agent/goals.py` | Goal eligibility, choice, single active plan, success detection, goal rule corrections |
| `src/agent/actions.py` | Turns a goal into a home page, at most one message, or a recorded "do nothing" |
| `src/agent/verifier.py` | Independent checks on raw data; the only thing that can approve an action |
| `src/agent/model.py` | Provider interface, mock advisor with fault injection, defensive parsing, circuit breaker |
| `src/agent/deps.py` | Data-store wrapper: tool-call counting, per-decision cache, injected slowness and timeouts |
| `src/agent/agent.py` | The per-viewer loop, live-event revisions, title withdrawal repair, rule corrections |
| `src/agent/judge.py` | Judges goals from outcome events, attribution, lessons and their feedback into decisions |
| `src/agent/impact.py` | Holdout comparison, confidence intervals, A/A check, ground truth, misleading comparisons |
| `src/replay.py` | The replay harness and CLI |
| `tests/` | Unit tests (`unittest`) |
| `sample_run/` | Output of a full replay |

## Documents

- [ARCHITECTURE.md](ARCHITECTURE.md): ownership, planning, memory, verification, latency, failure recovery, trust boundaries
- [AI_COLLABORATION.md](AI_COLLABORATION.md): how AI tools were used and checked
- [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md): what is intentionally incomplete
