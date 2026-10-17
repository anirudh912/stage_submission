# Impact report

Unit: each triggered viewer, in the arm `replay/arms.csv` randomized them to. Metric: the viewer's first goal (chosen by the same arm-blind rules in both arms; holdout goals are *shadow* goals) is reached inside its window, judged only from the simulator's outcome events.

## 1. Headline (intention-to-treat)

- Agent vs holdout: 13.6% vs 11.5% · **+2.1 pts** [95% CI -1.0, +5.3] · p=0.1843 · n=840/865
- Across 5 simulator seeds: effect +1.5 to +3.8 pts (mean +2.4). Seeds re-simulate the same viewers, so they show simulator noise, not extra evidence.
- **A/A check** (same goals, simulator run with *no* actions): 10.6% vs 11.5% · **-0.8 pts** [95% CI -3.8, +2.1] · p=0.5754 · n=840/865. This should be about zero; it shows that goal assignment and judging are not biased between arms.
- Dropping inconclusive goals (0 in this run): 13.6% vs 11.5% · **+2.1 pts** [95% CI -1.0, +5.3] · p=0.1843 · n=840/865
- 81% of agent-arm viewers actually saw an action. If the effect only comes through exposure, the effect on exposed viewers is about +2.6 pts (ITT / exposure rate). This is an estimate that leans on that assumption; the ITT number above does not.

### Simulator ground truth (only possible in simulation)

- Same agent-arm viewers, same seed, with vs without our actions: 89 → 114 goals reached (**+2.98 pts**; 25 viewers gained, 0 lost).
- Holdout viewers: 99 → 99 (0 gained, 0 lost): nothing leaked into the holdout.
- The ITT estimate (+2.13) differs from the truth by the arms' baseline gap (A/A -0.85); the ITT interval contains the true effect: True.

### Sharper estimates of the same effect (A1 + A2)

The question is unchanged (agent vs holdout, first goal reached). These estimators only remove noise that comes from viewers differing in how active they already were.

| Estimator | Effect | 95% CI | p | A/A (no actions) |
|---|---|---|---|---|
| Plain difference | +2.13 pts | -1.02 to +5.27 | 0.1848 | -0.85 |
| Adjusted for pre-replay activity (CUPED) | +1.47 pts | -1.60 to +4.54 | 0.3481 | -1.32 |
| Stratified by goal | +1.54 pts | -1.45 to +4.53 | 0.3124 | -1.31 |
| Both | +1.22 pts | -1.79 to +4.22 | 0.4269 | -1.54 |

- Covariates (history before the replay day, unaffected by the agent): active_days, play_days, completions, ever_paid, days_known. Variance removed by CUPED: 4%.
- Baseline balance (agent / holdout): active_days 7.288 / 7.486; play_days 3.73 / 3.792; completions 0.816 / 0.854; ever_paid 0.369 / 0.384; days_known 22.799 / 23.616
- Simulator ground truth for comparison: +2.98 pts.
- **Power:** with 840/865 viewers the smallest effect this test can reliably detect (80% power, 5% level) is about 4.5 pts. An effect of +3.0 pts is found only about 46% of the time; it needs about 1933 viewers per arm. Better estimators cannot close that gap here (they removed only 4% of the variance): it needs more viewers or a longer run.
- The plain number stays the headline. The adjusted ones are reported next to it, never instead of it, and the A/A column shows each method gives about zero when nothing was done.

## 2. By goal (first goal per viewer)

| Goal | Agent | Holdout | Diff (pts) | 95% CI | p |
|---|---|---|---|---|---|
| G01 | 39.3% (n=61) | 28.3% (n=53) | +11.0 | -6.2 to +28.3 | 0.2152 |
| G02 | 13.8% (n=269) | 11.4% (n=254) | +2.3 | -3.3 to +8.0 | 0.4211 |
| G04 | 0.0% (n=1) | 0.0% (n=1) | +0.0 | +0.0 to +0.0 | 1.0 |
| G05 | 4.2% (n=72) | 8.3% (n=72) | -4.2 | -12.0 to +3.7 | 0.3017 |
| G06 | 4.5% (n=22) | 6.7% (n=15) | -2.1 | -17.4 to +13.2 | 0.7794 |
| G07 | 2.9% (n=307) | 3.9% (n=361) | -0.9 | -3.7 to +1.8 | 0.5037 |
| G08 | 36.0% (n=86) | 30.6% (n=85) | +5.5 | -8.7 to +19.6 | 0.449 |
| G09 | 42.1% (n=19) | 29.4% (n=17) | +12.7 | -18.3 to +43.7 | 0.4286 |
| G12 | 50.0% (n=2) | 42.9% (n=7) | +7.1 | -71.2 to +85.5 | 0.8577 |

Small groups have wide intervals; do not read a per-goal winner from them.

### Goal fit: are we choosing goals our actions can move? (simulator ground truth)

Same agent-arm viewers with vs without our actions. *Chosen* = the goal the agent picked; *alternative* = another goal the same viewer was eligible for at that moment, judged on the same outcomes (our actions were built for the chosen goal, not for the alternative).

| Chosen goal | Viewers | Without us | With us | True lift |
|---|---|---|---|---|
| G07 | 307 | 2.3% | 2.9% | +0.7 pts (+2) |
| G02 | 269 | 11.2% | 13.8% | +2.6 pts (+7) |
| G08 | 86 | 29.1% | 36.0% | +7.0 pts (+6) |
| G05 | 72 | 4.2% | 4.2% | +0.0 pts (+0) |
| G01 | 61 | 23.0% | 39.3% | +16.4 pts (+10) |
| G06 | 22 | 4.5% | 4.5% | +0.0 pts (+0) |
| G09 | 19 | 42.1% | 42.1% | +0.0 pts (+0) |
| G12 | 2 | 50.0% | 50.0% | +0.0 pts (+0) |
| G03 | 1 | 0.0% | 0.0% | +0.0 pts (+0) |
| G04 | 1 | 0.0% | 0.0% | +0.0 pts (+0) |

| Chose | Also eligible | Viewers | Alternative reached, without → with us | Lift |
|---|---|---|---|---|
| G02 | G05 | 203 | 2.0% → 2.5% | +0.5 pts |
| G07 | G08 | 170 | 31.8% → 37.1% | +5.3 pts |
| G07 | G05 | 98 | 9.2% → 9.2% | +0.0 pts |
| G01 | G02 | 61 | 13.1% → 26.2% | +13.1 pts |
| G01 | G03 | 50 | 4.0% → 4.0% | +0.0 pts |
| G02 | G12 | 43 | 39.5% → 44.2% | +4.7 pts |
| G02 | G08 | 30 | 20.0% → 20.0% | +0.0 pts |
| G07 | G09 | 30 | 33.3% → 43.3% | +10.0 pts |
| G07 | G06 | 28 | 14.3% → 14.3% | +0.0 pts |
| G02 | G09 | 24 | 8.3% → 16.7% | +8.3 pts |
| G09 | G08 | 18 | 44.4% → 44.4% | +0.0 pts |

- **Why goals differ:** in the simulator a home page raises the chance that a viewer plays *something*; the title played is drawn from the dialect's catalogue. Goals counted on any play or completion (G01, G02, G08, G09) respond; G07 needs one specific series, which only a push click delivers; payment goals (G05, G06) respond only through tiny purchase odds.
- **Finding:** active subscribers with an unfinished series get G07 by priority, but our actions move their G08 (new release) outcome more. We **kept** the priority: a hero for the series a viewer left is the right real-world choice, and re-ranking goals on this simulator's quirk (one seed, one day, small groups) would be tuning to the test. Documented in KNOWN_LIMITATIONS.md.

## 3. Behaviour over 14 days (every triggered viewer)

- viewers with play: 77.5% vs 75.8% · **+1.8 pts** [95% CI -2.2, +5.7] · p=0.386 · n=863/883
- viewers with complete: 57.0% vs 56.9% · **+0.2 pts** [95% CI -4.5, +4.8] · p=0.9466 · n=863/883
- viewers with paid: 4.1% vs 4.1% · **-0.0 pts** [95% CI -1.9, +1.8] · p=0.9819 · n=863/883
- Mean active days: agent 5.446 vs holdout 5.352

## 4. Why simpler comparisons would mislead

- **Served vs everyone else** (the pilot report's method): 15.7% vs 10.2% · **+5.5 pts** [95% CI +2.2, +8.8] · p=0.0007 · n=706/999. Who gets served is not random: the agent abstains for stale triggers, missing dialects and holdout viewers, so 'served' selects viewers who were present and active. The gap mixes that selection with any real effect.
- **Exposed vs unexposed inside the agent arm**: 16.1% vs 3.1% · **+13.0 pts** [95% CI +9.2, +16.9] · p=0.0 · n=677/163. Seeing the page requires coming back to the home screen while it is live, which is itself a sign of an engaged viewer.
- **Counting 'credited' successes**: agent-arm attribution {'failed_after_exposure': 568, 'success_after_exposure': 105, 'not_served': 134, 'success_without_exposure': 6, 'failed_without_exposure': 27}. Many successes happen with no exposure, or would have happened anyway; per-viewer credit is not impact.
- Only the randomized arm comparison (section 1) removes these selection effects, because the arm was assigned before anything about the viewer was known.

## 5. The earlier pilot, re-measured

- Report's method, served vs all others: 40.1% vs 17.7% · **+22.4 pts** [95% CI +15.8, +29.1] · p=0.0 · n=217/3674
- Randomized treatment vs holdout: 18.2% vs 19.6% · **-1.3 pts** [95% CI -3.8, +1.2] · p=0.2998 · n=1912/1979
- Goal mix of served viewers {'G09': 105, 'G05': 84, 'G02': 15, 'G06': 12, 'G04': 1} vs others {'G05': 2034, 'G06': 319, 'G09': 848, 'G02': 345, 'G04': 128}: the served group is also a different mix of goals, so the 2.3x headline compares different populations and different goals. The randomized comparison shows no benefit.

## 6. Run facts

- Goal outcomes (all goals, both arms): {'failed': 1492, 'succeeded': 213}
- Simulator counts: {'actions_total': 1434, 'home_page_accepted': 912, 'home_page_published_but_not_shown': 14, 'home_page_shown_personalised': 827, 'organic_purchases_scripted': 36, 'push_clicked': 4, 'push_delivered': 318, 'push_requested': 363, 'push_undeliverable_token': 45, 'whatsapp_clicked': 4, 'whatsapp_delivered': 159, 'whatsapp_requested': 159}
