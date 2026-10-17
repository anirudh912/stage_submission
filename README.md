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

_In progress: one command that replays the trigger script and writes `sample_run/`._

## Layout

| Path | What |
|---|---|
| `src/prep_data.py` | Dedupes events, corrects phone clocks, flags late events, invalid dialects and injected text |
| `src/agent/store.py` | Durable per-viewer state (SQLite): events, profile versions, facts, goals, decisions, actions, learnings |
| `src/agent/resolver.py` | Turns events and profile rows into facts with source, freshness, confidence and conflicts |
| `src/agent/goals.py` | Goal eligibility, choice, single active plan, success detection, goal rule corrections |
| `tests/` | Unit tests (`unittest`) |
| `sample_run/` | Output of a full replay |

## Documents

- [ARCHITECTURE.md](ARCHITECTURE.md): ownership, planning, memory, verification, latency, failure recovery, trust boundaries
- [AI_COLLABORATION.md](AI_COLLABORATION.md): how AI tools were used and checked
- [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md): what is intentionally incomplete
