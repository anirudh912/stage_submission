# Tests

Run everything (standard library `unittest`, about 0.2 s, no data pack needed except `goals.json`/config if present):

```bash
python3 -m unittest discover -s tests -v
```

## The four scenarios the brief requires

| Requirement | Test (in `test_required_scenarios.py`) |
|---|---|
| Late or duplicate trigger | `test_1a_duplicate_trigger_is_idempotent`, `test_1b_late_buffered_trigger_does_not_act_as_if_the_viewer_were_here` |
| Conflicting source | `test_2_three_dialect_sources_disagree_and_a_purchase_beats_the_snapshot` |
| Tool failure | `test_3a_model_provider_down_falls_back_to_rules`, `test_3b_data_store_timeouts_degrade_to_a_safe_no_action` |
| Corrected goal rule | `test_4_corrected_g03_rule_repoints_only_open_g03_goals_and_judging_uses_it` |

## By area

| File | Covers |
|---|---|
| `test_state_and_facts.py` | Durable store (idempotent events, one open goal, one decision per trigger), source precedence, conflicts, no use of future events, wrong phone clocks, language variants |
| `test_goals.py` | Eligibility with evidence, unmeasurable goals, conflicting status blocks payment goals, single active plan, rule correction scope, IST-day and trial rules |
| `test_actions_and_verifier.py` | Doing nothing (holdout, stale trigger, no content, live page), page rules, injected and withdrawn titles, thin-catalogue rows, series leading the next_episode row, G07 skipped for a series that cannot be promoted, message timing, in-app message for viewers without consent and its daily cap; every verifier check catching a bad action, incl. an unsupported model claim |
| `test_agent_loop.py` | Advisor faults (unsupported claim, malformed, timeout, outage + circuit breaker), data-store degradation (incl. the catalogue-only fallback page and when it is refused), purchase after decision (push and in-app cancelled; in-app not cancelled once shown), title withdrawal repair |
| `test_judge_impact.py` | Judging and attribution, lessons changing the next decision, proportion maths, ITT unit, selection bias demo, ground-truth leakage flag, CUPED/stratification, honest push copy |
| `test_required_scenarios.py` | The four required scenarios, end to end |

`tests/helpers.py` builds a small hand-made catalogue and viewers, so tests do not depend on the full pack.
