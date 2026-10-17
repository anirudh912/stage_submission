# Impact report

Unit: each triggered viewer, in the arm `replay/arms.csv` randomized them to. Metric: the viewer's first goal (chosen by the same arm-blind rules in both arms; holdout goals are *shadow* goals) is reached inside its window, judged only from the simulator's outcome events.

## 1. Headline (intention-to-treat)

- Agent vs holdout: 16.0% vs 12.0% · **+4.0 pts** [95% CI +0.7, +7.2] · p=0.0187 · n=838/864
- Across 5 simulator seeds: effect +1.9 to +4.3 pts (mean +3.2). Seeds re-simulate the same viewers, so they show simulator noise, not extra evidence.
- **A/A check** (same goals, simulator run with *no* actions): 12.0% vs 12.0% · **+0.0 pts** [95% CI -3.1, +3.1] · p=0.9922 · n=838/864. This should be about zero; it shows that goal assignment and judging are not biased between arms.
- Dropping inconclusive goals (0 in this run): 16.0% vs 12.0% · **+4.0 pts** [95% CI +0.7, +7.2] · p=0.0187 · n=838/864
- 81% of agent-arm viewers actually saw an action. If the effect only comes through exposure, the effect on exposed viewers is about +4.9 pts (ITT / exposure rate). This is an estimate that leans on that assumption; the ITT number above does not.

### Simulator ground truth (only possible in simulation)

- Same agent-arm viewers, same seed, with vs without our actions: 101 → 134 goals reached (**+3.94 pts**; 33 viewers gained, 0 lost).
- Holdout viewers: 104 → 104 (0 gained, 0 lost): nothing leaked into the holdout.
- The ITT estimate (+3.95) differs from the truth by the arms' baseline gap (A/A +0.02); the ITT interval contains the true effect: True.

### Sharper estimates of the same effect (A1 + A2)

The question is unchanged (agent vs holdout, first goal reached). These estimators only remove noise that comes from viewers differing in how active they already were.

| Estimator | Effect | 95% CI | p | A/A (no actions) |
|---|---|---|---|---|
| Plain difference | +3.95 pts | +0.66 to +7.25 | 0.0188 | +0.02 |
| Adjusted for pre-replay activity (CUPED) | +3.38 pts | +0.15 to +6.60 | 0.04 | -0.38 |
| Stratified by goal | +3.06 pts | -0.08 to +6.21 | 0.0564 | -0.72 |
| Both | +2.84 pts | -0.32 to +6.00 | 0.0778 | -0.86 |

- Covariates (history before the replay day, unaffected by the agent): active_days, play_days, completions, ever_paid, days_known. Variance removed by CUPED: 4%.
- Baseline balance (agent / holdout): active_days 7.284 / 7.493; play_days 3.714 / 3.796; completions 0.812 / 0.855; ever_paid 0.368 / 0.384; days_known 22.81 / 23.642
- Simulator ground truth for comparison: +3.94 pts.
- **Power:** with 838/864 viewers the smallest effect this test can reliably detect (80% power, 5% level) is about 4.7 pts. An effect of +3.9 pts is found only about 65% of the time; it needs about 1218 viewers per arm. Better estimators cannot close that gap here (they removed only 4% of the variance): it needs more viewers or a longer run.
- The plain number stays the headline. The adjusted ones are reported next to it, never instead of it, and the A/A column shows each method gives about zero when nothing was done.

## 2. By goal (first goal per viewer)

| Goal | Agent | Holdout | Diff (pts) | 95% CI | p |
|---|---|---|---|---|---|
| G01 | 39.3% (n=61) | 26.9% (n=52) | +12.4 | -4.8 to +29.6 | 0.1636 |
| G02 | 14.5% (n=269) | 11.4% (n=254) | +3.1 | -2.7 to +8.8 | 0.2951 |
| G04 | 0.0% (n=1) | 0.0% (n=1) | +0.0 | +0.0 to +0.0 | 1.0 |
| G05 | 3.7% (n=82) | 6.8% (n=88) | -3.2 | -9.8 to +3.5 | 0.3579 |
| G06 | 8.3% (n=24) | 5.0% (n=20) | +3.3 | -11.3 to +17.9 | 0.6623 |
| G07 | 4.7% (n=257) | 4.5% (n=313) | +0.2 | -3.2 to +3.6 | 0.911 |
| G08 | 36.4% (n=118) | 26.9% (n=108) | +9.6 | -2.5 to +21.6 | 0.1222 |
| G09 | 43.5% (n=23) | 38.1% (n=21) | +5.4 | -23.6 to +34.4 | 0.7168 |
| G12 | 50.0% (n=2) | 42.9% (n=7) | +7.1 | -71.2 to +85.5 | 0.8577 |

Small groups have wide intervals; do not read a per-goal winner from them.

### Goal fit: are we choosing goals our actions can move? (simulator ground truth)

Same agent-arm viewers with vs without our actions. *Chosen* = the goal the agent picked; *alternative* = another goal the same viewer was eligible for at that moment, judged on the same outcomes (our actions were built for the chosen goal, not for the alternative).

| Chosen goal | Viewers | Without us | With us | True lift |
|---|---|---|---|---|
| G02 | 269 | 11.2% | 14.5% | +3.3 pts (+9) |
| G07 | 257 | 2.7% | 4.7% | +1.9 pts (+5) |
| G08 | 118 | 29.7% | 36.4% | +6.8 pts (+8) |
| G05 | 82 | 3.7% | 3.7% | +0.0 pts (+0) |
| G01 | 61 | 23.0% | 39.3% | +16.4 pts (+10) |
| G06 | 24 | 8.3% | 8.3% | +0.0 pts (+0) |
| G09 | 23 | 39.1% | 43.5% | +4.3 pts (+1) |
| G12 | 2 | 50.0% | 50.0% | +0.0 pts (+0) |
| G03 | 1 | 0.0% | 0.0% | +0.0 pts (+0) |
| G04 | 1 | 0.0% | 0.0% | +0.0 pts (+0) |

| Chose | Also eligible | Viewers | Alternative reached, without → with us | Lift |
|---|---|---|---|---|
| G02 | G05 | 203 | 2.0% → 2.5% | +0.5 pts |
| G07 | G08 | 132 | 32.6% → 39.4% | +6.8 pts |
| G07 | G05 | 89 | 10.1% → 10.1% | +0.0 pts |
| G01 | G02 | 61 | 13.1% → 26.2% | +13.1 pts |
| G01 | G03 | 50 | 4.0% → 4.0% | +0.0 pts |
| G02 | G12 | 43 | 39.5% → 44.2% | +4.7 pts |
| G02 | G08 | 30 | 20.0% → 20.0% | +0.0 pts |
| G07 | G06 | 26 | 11.5% → 11.5% | +0.0 pts |
| G07 | G09 | 26 | 34.6% → 42.3% | +7.7 pts |
| G02 | G09 | 24 | 8.3% → 16.7% | +8.3 pts |
| G09 | G08 | 22 | 36.4% → 40.9% | +4.5 pts |

- **Why goals differ:** in the simulator a home page raises the chance that a viewer plays *something*; the title played is drawn from the dialect's catalogue. Goals counted on any play or completion (G01, G02, G08, G09) respond; G07 needs one specific series, which only a push click delivers; payment goals (G05, G06) respond only through tiny purchase odds.
- **Finding:** active subscribers with an unfinished series get G07 by priority, but our actions move their G08 (new release) outcome more. We **kept** the priority: a hero for the series a viewer left is the right real-world choice, and re-ranking goals on this simulator's quirk (one seed, one day, small groups) would be tuning to the test. Documented in KNOWN_LIMITATIONS.md.

## 3. Behaviour over 14 days (every triggered viewer)

- viewers with play: 77.6% vs 75.8% · **+1.9 pts** [95% CI -2.1, +5.8] · p=0.3551 · n=863/883
- viewers with complete: 57.0% vs 56.9% · **+0.2 pts** [95% CI -4.5, +4.8] · p=0.9466 · n=863/883
- viewers with paid: 4.1% vs 4.1% · **-0.0 pts** [95% CI -1.9, +1.8] · p=0.9819 · n=863/883
- Mean active days: agent 5.447 vs holdout 5.352

## 4. Why simpler comparisons would mislead

- **Served vs everyone else** (the pilot report's method): 18.7% vs 10.6% · **+8.1 pts** [95% CI +4.6, +11.6] · p=0.0 · n=705/997. Who gets served is not random: the agent abstains for stale triggers, missing dialects and holdout viewers, so 'served' selects viewers who were present and active. The gap mixes that selection with any real effect.
- **Exposed vs unexposed inside the agent arm**: 19.1% vs 2.5% · **+16.6 pts** [95% CI +12.8, +20.4] · p=0.0 · n=680/158. Seeing the page requires coming back to the home screen while it is live, which is itself a sign of an engaged viewer.
- **Counting 'credited' successes**: agent-arm attribution {'failed_after_exposure': 550, 'success_after_exposure': 126, 'not_served': 133, 'success_without_exposure': 6, 'failed_without_exposure': 23}. Many successes happen with no exposure, or would have happened anyway; per-viewer credit is not impact.
- Only the randomized arm comparison (section 1) removes these selection effects, because the arm was assigned before anything about the viewer was known.

## 5. The earlier pilot, re-measured

- Report's method, served vs all others: 40.1% vs 17.7% · **+22.4 pts** [95% CI +15.8, +29.1] · p=0.0 · n=217/3674
- Randomized treatment vs holdout: 18.2% vs 19.6% · **-1.3 pts** [95% CI -3.8, +1.2] · p=0.2998 · n=1912/1979
- Goal mix of served viewers {'G09': 105, 'G05': 84, 'G02': 15, 'G06': 12, 'G04': 1} vs others {'G05': 2034, 'G06': 319, 'G09': 848, 'G02': 345, 'G04': 128}: the served group is also a different mix of goals, so the 2.3x headline compares different populations and different goals. The randomized comparison shows no benefit.

## 6. Run facts

- Goal outcomes (all goals, both arms): {'failed': 1465, 'succeeded': 238}
- Simulator counts: {'actions_total': 1606, 'home_page_accepted': 915, 'home_page_published_but_not_shown': 14, 'home_page_shown_personalised': 828, 'in_app_message_queued': 133, 'in_app_message_requested': 133, 'organic_purchases_scripted': 36, 'push_clicked': 6, 'push_delivered': 341, 'push_requested': 391, 'push_undeliverable_token': 50, 'whatsapp_clicked': 5, 'whatsapp_delivered': 167, 'whatsapp_requested': 167}
