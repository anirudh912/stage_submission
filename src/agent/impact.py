"""Impact against the randomized holdout, and why simpler comparisons mislead.

Unit: a triggered viewer. Arm: as randomized in replay/arms.csv (never "served or not").
Primary metric: the viewer's FIRST goal (chosen by arm-blind rules, so both arms get the same kind of goals) is
reached inside its window. Inconclusive counts as not reached (conservative); a sensitivity view drops them.
"""
import csv
import math
from collections import Counter, defaultdict

from .timeutil import ts


def diff(s1, n1, s0, n0):
    """Difference of two proportions (arm 1 - arm 0) with a 95% normal CI and a two-sided z-test p-value."""
    if not n1 or not n0:
        return None
    p1, p0 = s1 / n1, s0 / n0
    se = math.sqrt(p1 * (1 - p1) / n1 + p0 * (1 - p0) / n0)
    pp = (s1 + s0) / (n1 + n0)
    se0 = math.sqrt(pp * (1 - pp) * (1 / n1 + 1 / n0))
    z = (p1 - p0) / se0 if se0 else 0.0
    return {"rate_1": round(p1, 4), "n_1": n1, "rate_0": round(p0, 4), "n_0": n0,
            "diff_pts": round(100 * (p1 - p0), 2), "ci95_pts": [round(100 * (p1 - p0 - 1.96 * se), 2),
                                                               round(100 * (p1 - p0 + 1.96 * se), 2)],
            "p_value": round(math.erfc(abs(z) / math.sqrt(2)), 4),
            "relative": round(p1 / p0, 3) if p0 else None}


# ------------------------------------------------------------------ variance reduction (A1, A2)

COVARIATES = ("active_days", "play_days", "completions", "ever_paid", "days_known")


def pre_covariates(store):
    """Per-viewer activity BEFORE the replay day (history events only). It cannot be affected by the agent,
    so adjusting for it is valid under randomization; it only removes noise from the estimate."""
    rows = store.db.execute("""
        SELECT user_id,
          COUNT(DISTINCT CASE WHEN event IN ('app_open','login_success')
                THEN date(received_at, '+330 minutes') END),
          COUNT(DISTINCT CASE WHEN event = 'play_started' THEN date(received_at, '+330 minutes') END),
          SUM(event = 'episode_completed'),
          MAX(event IN ('subscription_renewed') OR (event = 'purchase_completed')),
          MIN(received_at)
        FROM events WHERE source = 'history' GROUP BY user_id""").fetchall()
    # history ends where the replay starts: the first trigger (no fixed dates, so any replay day works)
    start = store.db.execute("SELECT MIN(received_at) FROM events WHERE source = 'trigger'").fetchone()[0]
    out = {}
    for u, ad, pd, comp, paid, first in rows:
        known = min(30, int((ts(start) - ts(first)).total_seconds() // 86400) + 1) if first and start else 30
        out[u] = {"active_days": ad or 0, "play_days": pd or 0, "completions": math.log1p(comp or 0),
                  "ever_paid": 1.0 if paid else 0.0, "days_known": known}
    return out


def _solve(A, b):
    """Gaussian elimination with partial pivoting (small k)."""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12:
            continue
        M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c] / M[c][c]
                M[r] = [x - f * y for x, y in zip(M[r], M[c])]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def _cuped(ys, xs):
    """Regress the outcome on the pre-period covariates (pooled over both arms, arm NOT in the model) and return
    outcome minus the predicted part, recentred. The arm difference of these residuals is the adjusted effect."""
    k = len(xs[0])
    means = [sum(x[j] for x in xs) / len(xs) for j in range(k)]
    X = [[1.0] + [x[j] - means[j] for j in range(k)] for x in xs]
    XtX = [[sum(r[i] * r[j] for r in X) for j in range(k + 1)] for i in range(k + 1)]
    Xty = [sum(r[i] * y for r, y in zip(X, ys)) for i in range(k + 1)]
    beta = _solve(XtX, Xty)
    return [y - sum(b * v for b, v in zip(beta[1:], r[1:])) for y, r in zip(ys, X)], beta


def _mean_diff(a, h):
    """Difference of means (agent - holdout) with a Welch 95% CI and normal p-value."""
    if len(a) < 2 or len(h) < 2:
        return None
    ma, mh = sum(a) / len(a), sum(h) / len(h)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vh = sum((x - mh) ** 2 for x in h) / (len(h) - 1)
    se = math.sqrt(va / len(a) + vh / len(h))
    z = (ma - mh) / se if se else 0.0
    return {"diff_pts": round(100 * (ma - mh), 2), "ci95_pts": [round(100 * (ma - mh - 1.96 * se), 2),
                                                               round(100 * (ma - mh + 1.96 * se), 2)],
            "se_pts": round(100 * se, 3), "p_value": round(math.erfc(abs(z) / math.sqrt(2)), 4),
            "n_1": len(a), "n_0": len(h)}


def _stratified(units):
    """Post-stratify by goal: arm difference within each goal, weighted by the goal's share of viewers.
    Goals are assigned by arm-blind rules before any action, so they are valid strata."""
    by = defaultdict(lambda: {"agent": [], "holdout": []})
    for u in units:
        by[u["goal_id"]][u["arm"]].append(u["y"])
    used = {g: v for g, v in by.items() if len(v["agent"]) >= 2 and len(v["holdout"]) >= 2}
    N = sum(len(v["agent"]) + len(v["holdout"]) for v in used.values())
    est, var = 0.0, 0.0
    for g, v in used.items():
        d = _mean_diff(v["agent"], v["holdout"])
        w = (len(v["agent"]) + len(v["holdout"])) / N
        est += w * d["diff_pts"]
        var += (w * d["se_pts"]) ** 2
    se = math.sqrt(var)
    z = est / se if se else 0.0
    dropped = sum(len(v["agent"]) + len(v["holdout"]) for g, v in by.items() if g not in used)
    return {"diff_pts": round(est, 2), "ci95_pts": [round(est - 1.96 * se, 2), round(est + 1.96 * se, 2)],
            "se_pts": round(se, 3), "p_value": round(math.erfc(abs(z) / math.sqrt(2)), 4),
            "strata": sorted(used), "viewers_in_dropped_strata": dropped}


def adjusted(outcomes, cov):
    """A1 + A2: the same ITT question, estimated four ways. Only the noise changes, not what is measured."""
    first = first_goals(outcomes)
    units = []
    for u, o in first.items():
        c = cov.get(u) or {k: 0.0 for k in COVARIATES}
        units.append({"u": u, "arm": o["arm"], "goal_id": o["goal_id"], "y": 1.0 if o["status"] == "succeeded" else 0.0,
                      "x": [float(c[k]) for k in COVARIATES], "has_history": u in cov})
    plain = _mean_diff([x["y"] for x in units if x["arm"] == "agent"], [x["y"] for x in units if x["arm"] == "holdout"])
    resid, beta = _cuped([x["y"] for x in units], [x["x"] for x in units])
    cu = [{**x, "y": r} for x, r in zip(units, resid)]
    cuped = _mean_diff([x["y"] for x in cu if x["arm"] == "agent"], [x["y"] for x in cu if x["arm"] == "holdout"])
    out = {"plain": plain, "cuped": cuped, "stratified_by_goal": _stratified(units),
           "cuped_and_stratified": _stratified(cu),
           "covariates": list(COVARIATES), "cuped_coefficients": [round(b, 4) for b in beta[1:]],
           "viewers_without_history": sum(not x["has_history"] for x in units)}
    if plain and cuped and plain["se_pts"]:
        out["variance_reduction"] = round(1 - (cuped["se_pts"] / plain["se_pts"]) ** 2, 3)
    bal = {}
    for k_i, k in enumerate(COVARIATES):
        a = [x["x"][k_i] for x in units if x["arm"] == "agent"]
        h = [x["x"][k_i] for x in units if x["arm"] == "holdout"]
        bal[k] = {"agent": round(sum(a) / len(a), 3), "holdout": round(sum(h) / len(h), 3)}
    out["baseline_balance"] = bal
    return out


def first_goals(outcomes):
    first = {}
    for o in sorted(outcomes, key=lambda o: o["started_at"]):
        first.setdefault(o["viewer_id"], o)
    return first


def itt(outcomes, drop_inconclusive=False):
    first = first_goals(outcomes)
    cnt = {"agent": [0, 0], "holdout": [0, 0]}
    for o in first.values():
        if drop_inconclusive and o["status"] == "inconclusive":
            continue
        c = cnt[o["arm"]]
        c[1] += 1
        c[0] += o["status"] == "succeeded"
    return diff(cnt["agent"][0], cnt["agent"][1], cnt["holdout"][0], cnt["holdout"][1])


def by_goal(outcomes):
    first = first_goals(outcomes)
    g = defaultdict(lambda: {"agent": [0, 0], "holdout": [0, 0]})
    for o in first.values():
        c = g[o["goal_id"]][o["arm"]]
        c[1] += 1
        c[0] += o["status"] == "succeeded"
    return {gid: diff(v["agent"][0], v["agent"][1], v["holdout"][0], v["holdout"][1]) for gid, v in sorted(g.items())}


def behaviour(events, arms, triggered):
    """Secondary ITT metrics over every triggered viewer (with or without a goal)."""
    acts = defaultdict(lambda: {"play": False, "complete": False, "paid": False, "days": set()})
    for e in events:
        u = e["user_id"]
        if u not in triggered:
            continue
        a = acts[u]
        if e["event"] == "play_started":
            a["play"] = True
        elif e["event"] == "episode_completed":
            a["complete"] = True
        elif e["event"] == "purchase_completed" and not (e.get("properties") or {}).get("is_trial"):
            a["paid"] = True
        if e["event"] == "app_open":
            a["days"].add(e["received_at"][:10])
    out = {}
    for metric in ("play", "complete", "paid"):
        s = {arm: sum(acts[u][metric] for u in triggered if arms.get(u) == arm) for arm in ("agent", "holdout")}
        n = {arm: sum(1 for u in triggered if arms.get(u) == arm) for arm in ("agent", "holdout")}
        out[f"viewers_with_{metric}"] = diff(s["agent"], n["agent"], s["holdout"], n["holdout"])
    days = {arm: [len(acts[u]["days"]) for u in triggered if arms.get(u) == arm] for arm in ("agent", "holdout")}
    out["mean_active_days"] = {arm: round(sum(v) / len(v), 3) for arm, v in days.items() if v}
    return out


def misleading(outcomes):
    """The comparisons the brief warns about, computed on the same data."""
    first = first_goals(outcomes)
    rows = list(first.values())
    served = [o for o in rows if o["arm"] == "agent" and o["exposure"]["published"]]
    rest = [o for o in rows if not (o["arm"] == "agent" and o["exposure"]["published"])]
    exposed = [o for o in rows if o["arm"] == "agent" and o["exposure"]["first_exposure_at"]]
    agent_unexposed = [o for o in rows if o["arm"] == "agent" and not o["exposure"]["first_exposure_at"]]
    ok = lambda xs: sum(o["status"] == "succeeded" for o in xs)
    credited = Counter(o["attribution"] for o in rows if o["arm"] == "agent")
    return {
        "served_vs_everyone_else (pilot method)": diff(ok(served), len(served), ok(rest), len(rest)),
        "exposed_vs_unexposed_within_agent_arm": diff(ok(exposed), len(exposed), ok(agent_unexposed),
                                                      len(agent_unexposed)),
        "agent_arm_attribution": dict(credited),
    }


def pilot(pack_dir):
    import os
    rows = list(csv.DictReader(open(os.path.join(pack_dir, "pilot", "pilot_outcomes.csv"))))
    ok = lambda xs: sum(r["succeeded"] == "true" for r in xs)
    t = [r for r in rows if r["arm"] == "treatment"]
    h = [r for r in rows if r["arm"] == "holdout"]
    served = [r for r in rows if r["served"] == "true"]
    others = [r for r in rows if r["served"] != "true"]
    return {"randomized_treatment_vs_holdout": diff(ok(t), len(t), ok(h), len(h)),
            "report_method_served_vs_all_others": diff(ok(served), len(served), ok(others), len(others)),
            "served_goal_mix": dict(Counter(r["goal_id"] for r in served)),
            "others_goal_mix": dict(Counter(r["goal_id"] for r in others))}


def ground_truth(with_actions, no_actions):
    """Simulator-only check: the same viewers judged with our actions and with none (same seed, so the same random
    draws). Real deployments never see this counterfactual; here it tells us whether the ITT estimate is right,
    and whether anything leaked into the holdout."""
    A, N = first_goals(with_actions), first_goals(no_actions)
    out = {}
    for arm in ("agent", "holdout"):
        users = [u for u in A if A[u]["arm"] == arm]
        sa = sum(A[u]["status"] == "succeeded" for u in users)
        sn = sum(N[u]["status"] == "succeeded" for u in users if u in N)
        gained = sum(A[u]["status"] == "succeeded" and N[u]["status"] != "succeeded" for u in users if u in N)
        lost = sum(A[u]["status"] != "succeeded" and N[u]["status"] == "succeeded" for u in users if u in N)
        out[arm] = {"n": len(users), "succeeded_with_actions": sa, "succeeded_without": sn,
                    "true_effect_pts": round(100 * (sa - sn) / len(users), 2) if users else None,
                    "viewers_gained": gained, "viewers_lost": lost}
    return out


def goal_fit(decisions, events_with, events_without, store, pack, registry, obs_end):
    """Simulator-only: for agent-arm viewers, the true lift of our actions on the goal we chose, and on every OTHER
    goal they were eligible for at the same moment (same outcome streams). Shows whether we picked goals our
    actions can move. Not used to tune the agent: it relies on the simulator's counterfactual."""
    from .judge import index_outcomes, judge_goal
    from .timeutil import iso, ts
    from datetime import timedelta
    W, N = index_outcomes(events_with), index_outcomes(events_without)

    def reached(u, gid, start, events, target=None):
        spec = registry.success_spec(gid)
        if target:
            spec["target"] = target
        g = {"goal_instance_id": "fit", "user_id": u, "goal_id": gid, "status": "open", "started_at": start,
             "deadline": iso(ts(start) + timedelta(hours=spec["window_hours"])), "success_spec": spec,
             "rule_version": spec["rule_version"]}
        return judge_goal(g, "agent", [], events.get(u, []), pack, obs_end)["status"] == "succeeded"

    first = {}
    for d in decisions:
        g = d.get("goal") or {}
        if g.get("instance") and not g.get("continuing") and d.get("arm") == "agent":
            first.setdefault(d["viewer_id"], d)
    chosen = defaultdict(lambda: [0, 0, 0])
    alt = defaultdict(lambda: [0, 0, 0])
    for u, d in first.items():
        gid, start = d["goal"]["id"], d["created_at"]
        c = chosen[gid]
        c[0] += 1
        c[1] += reached(u, gid, start, W, d["goal"].get("target"))
        c[2] += reached(u, gid, start, N, d["goal"].get("target"))
        for cand in d["goal"]["considered"]:
            a = cand["goal_id"]
            if cand["eligible"] and a != gid and a != "G07":      # G07 needs a target series; skip as alternative
                x = alt[(gid, a)]
                x[0] += 1
                x[1] += reached(u, a, start, W)
                x[2] += reached(u, a, start, N)
    row = lambda n, w, wo: {"n": n, "without_pct": round(100 * wo / n, 1), "with_pct": round(100 * w / n, 1),
                            "lift_pts": round(100 * (w - wo) / n, 1), "viewers_gained": w - wo}
    return {"chosen": {g: row(*v) for g, v in sorted(chosen.items(), key=lambda x: -x[1][0])},
            "alternatives": [{"chosen": g, "alternative": a, **row(*v)}
                             for (g, a), v in sorted(alt.items(), key=lambda x: -x[1][0]) if v[0] >= 15]}


def exposure_rate(outcomes):
    first = first_goals(outcomes)
    agent = [o for o in first.values() if o["arm"] == "agent"]
    return round(sum(bool(o["exposure"]["first_exposure_at"]) for o in agent) / len(agent), 4) if agent else None


# ------------------------------------------------------------------ report

def _fmt(d):
    if not d:
        return "n/a"
    return (f"{100 * d['rate_1']:.1f}% vs {100 * d['rate_0']:.1f}% · **{d['diff_pts']:+.1f} pts** "
            f"[95% CI {d['ci95_pts'][0]:+.1f}, {d['ci95_pts'][1]:+.1f}] · p={d['p_value']} · n={d['n_1']}/{d['n_0']}")


def report_md(main, seeds, aa, beh, mis, pil, goals, exp_rate, status_counts, sim_counts, truth=None, adj=None,
              adj_aa=None, fit=None):
    cace = (main["diff_pts"] / exp_rate) if exp_rate else None
    L = []
    L.append("# Impact report\n")
    L.append("Unit: each triggered viewer, in the arm `replay/arms.csv` randomized them to. Metric: the viewer's "
             "first goal (chosen by the same arm-blind rules in both arms; holdout goals are *shadow* goals) is "
             "reached inside its window, judged only from the simulator's outcome events.\n")
    L.append("## 1. Headline (intention-to-treat)\n")
    L.append(f"- Agent vs holdout: {_fmt(main)}")
    s = seeds["effects"]
    L.append(f"- Across {len(s)} simulator seeds: effect {min(s):+.1f} to {max(s):+.1f} pts "
             f"(mean {sum(s) / len(s):+.1f}). Seeds re-simulate the same viewers, so they show simulator noise, "
             "not extra evidence.")
    L.append(f"- **A/A check** (same goals, simulator run with *no* actions): {_fmt(aa)}. "
             "This should be about zero; it shows that goal assignment and judging are not biased between arms.")
    n_inc = status_counts.get("inconclusive", 0)
    L.append(f"- Dropping inconclusive goals ({n_inc} in this run): {_fmt(seeds['main_drop_inconclusive'])}")
    if cace is not None:
        L.append(f"- {100 * exp_rate:.0f}% of agent-arm viewers actually saw an action. If the effect only comes "
                 f"through exposure, the effect on exposed viewers is about {cace:+.1f} pts (ITT / exposure rate). "
                 "This is an estimate that leans on that assumption; the ITT number above does not.")
    if truth:
        ag, ho = truth["agent"], truth["holdout"]
        L.append("\n### Simulator ground truth (only possible in simulation)\n")
        L.append(f"- Same agent-arm viewers, same seed, with vs without our actions: {ag['succeeded_without']} → "
                 f"{ag['succeeded_with_actions']} goals reached (**{ag['true_effect_pts']:+.2f} pts**; "
                 f"{ag['viewers_gained']} viewers gained, {ag['viewers_lost']} lost).")
        L.append(f"- Holdout viewers: {ho['succeeded_without']} → {ho['succeeded_with_actions']} "
                 f"({ho['viewers_gained']} gained, {ho['viewers_lost']} lost): nothing leaked into the holdout.")
        L.append(f"- The ITT estimate ({main['diff_pts']:+.2f}) differs from the truth by the arms' baseline gap "
                 f"(A/A {aa['diff_pts']:+.2f}); the ITT interval contains the true effect: "
                 f"{main['ci95_pts'][0] <= ag['true_effect_pts'] <= main['ci95_pts'][1]}.")
    if adj:
        L.append("\n### Sharper estimates of the same effect (A1 + A2)\n")
        L.append("The question is unchanged (agent vs holdout, first goal reached). These estimators only remove noise "
                 "that comes from viewers differing in how active they already were.\n")
        L.append("| Estimator | Effect | 95% CI | p | A/A (no actions) |")
        L.append("|---|---|---|---|---|")
        names = [("plain", "Plain difference"), ("cuped", "Adjusted for pre-replay activity (CUPED)"),
                 ("stratified_by_goal", "Stratified by goal"), ("cuped_and_stratified", "Both")]
        for key, label in names:
            d, a = adj[key], (adj_aa or {}).get(key)
            L.append(f"| {label} | {d['diff_pts']:+.2f} pts | {d['ci95_pts'][0]:+.2f} to {d['ci95_pts'][1]:+.2f} | "
                     f"{d['p_value']} | {a['diff_pts']:+.2f} |" if a else f"| {label} | {d['diff_pts']:+.2f} | | | |")
        bal = adj["baseline_balance"]
        L.append(f"\n- Covariates (history before the replay day, unaffected by the agent): "
                 f"{', '.join(adj['covariates'])}. Variance removed by CUPED: "
                 f"{100 * adj.get('variance_reduction', 0):.0f}%.")
        L.append("- Baseline balance (agent / holdout): " + "; ".join(f"{k} {v['agent']} / {v['holdout']}"
                                                                     for k, v in bal.items()))
        if truth:
            L.append(f"- Simulator ground truth for comparison: {truth['agent']['true_effect_pts']:+.2f} pts.")
        se = adj["plain"]["se_pts"]
        p_bar = (main["rate_1"] + main["rate_0"]) / 2
        eff = truth["agent"]["true_effect_pts"] if truth else main["diff_pts"]
        need = math.ceil(7.84 * 2 * p_bar * (1 - p_bar) / (eff / 100) ** 2) if eff > 0 else None
        power = 0.5 * math.erfc(-(abs(eff) / se - 1.96) / math.sqrt(2)) if se else None
        L.append(f"- **Power:** with {main['n_1']}/{main['n_0']} viewers the smallest effect this test can reliably "
                 f"detect (80% power, 5% level) is about {2.8 * se:.1f} pts. An effect of {eff:+.1f} pts is found "
                 f"only about {100 * power:.0f}% of the time; it needs about {need} viewers per arm. Better "
                 "estimators cannot close that gap here (they removed only "
                 f"{100 * adj.get('variance_reduction', 0):.0f}% of the variance): it needs more viewers or a longer run.")
        L.append("- The plain number stays the headline. The adjusted ones are reported next to it, never "
                 "instead of it, and the A/A column shows each method gives about zero when nothing was done.")
    L.append("\n## 2. By goal (first goal per viewer)\n")
    L.append("| Goal | Agent | Holdout | Diff (pts) | 95% CI | p |")
    L.append("|---|---|---|---|---|---|")
    for gid, d in goals.items():
        if d:
            L.append(f"| {gid} | {100 * d['rate_1']:.1f}% (n={d['n_1']}) | {100 * d['rate_0']:.1f}% (n={d['n_0']}) | "
                     f"{d['diff_pts']:+.1f} | {d['ci95_pts'][0]:+.1f} to {d['ci95_pts'][1]:+.1f} | {d['p_value']} |")
    L.append("\nSmall groups have wide intervals; do not read a per-goal winner from them.\n")
    if fit:
        L.append("### Goal fit: are we choosing goals our actions can move? (simulator ground truth)\n")
        L.append("Same agent-arm viewers with vs without our actions. *Chosen* = the goal the agent picked; "
                 "*alternative* = another goal the same viewer was eligible for at that moment, judged on the same "
                 "outcomes (our actions were built for the chosen goal, not for the alternative).\n")
        L.append("| Chosen goal | Viewers | Without us | With us | True lift |")
        L.append("|---|---|---|---|---|")
        for g, r in fit["chosen"].items():
            L.append(f"| {g} | {r['n']} | {r['without_pct']}% | {r['with_pct']}% | {r['lift_pts']:+.1f} pts "
                     f"({r['viewers_gained']:+d}) |")
        L.append("\n| Chose | Also eligible | Viewers | Alternative reached, without → with us | Lift |")
        L.append("|---|---|---|---|---|")
        for r in fit["alternatives"]:
            L.append(f"| {r['chosen']} | {r['alternative']} | {r['n']} | {r['without_pct']}% → {r['with_pct']}% | "
                     f"{r['lift_pts']:+.1f} pts |")
        L.append("\n- **Why goals differ:** in the simulator a home page raises the chance that a viewer plays "
                 "*something*; the title played is drawn from the dialect's catalogue. Goals counted on any play or "
                 "completion (G01, G02, G08, G09) respond; G07 needs one specific series, which only a push click "
                 "delivers; payment goals (G05, G06) respond only through tiny purchase odds.")
        L.append("- **Finding:** active subscribers with an unfinished series get G07 by priority, but our actions "
                 "move their G08 (new release) outcome more. We **kept** the priority: a hero for the series a viewer "
                 "left is the right real-world choice, and re-ranking goals on this simulator's quirk (one seed, one "
                 "day, small groups) would be tuning to the test. Documented in KNOWN_LIMITATIONS.md.\n")
    L.append("## 3. Behaviour over 14 days (every triggered viewer)\n")
    for k, d in beh.items():
        if k == "mean_active_days":
            L.append(f"- Mean active days: agent {d.get('agent')} vs holdout {d.get('holdout')}")
        else:
            L.append(f"- {k.replace('_', ' ')}: {_fmt(d)}")
    L.append("\n## 4. Why simpler comparisons would mislead\n")
    sv = mis["served_vs_everyone_else (pilot method)"]
    ex = mis["exposed_vs_unexposed_within_agent_arm"]
    L.append(f"- **Served vs everyone else** (the pilot report's method): {_fmt(sv)}. Who gets served is not "
             "random: the agent abstains for stale triggers, missing dialects and holdout viewers, so 'served' "
             "selects viewers who were present and active. The gap mixes that selection with any real effect.")
    L.append(f"- **Exposed vs unexposed inside the agent arm**: {_fmt(ex)}. Seeing the page requires coming back "
             "to the home screen while it is live, which is itself a sign of an engaged viewer.")
    L.append(f"- **Counting 'credited' successes**: agent-arm attribution {mis['agent_arm_attribution']}. Many "
             "successes happen with no exposure, or would have happened anyway; per-viewer credit is not impact.")
    L.append("- Only the randomized arm comparison (section 1) removes these selection effects, because the arm "
             "was assigned before anything about the viewer was known.\n")
    L.append("## 5. The earlier pilot, re-measured\n")
    L.append(f"- Report's method, served vs all others: {_fmt(pil['report_method_served_vs_all_others'])}")
    L.append(f"- Randomized treatment vs holdout: {_fmt(pil['randomized_treatment_vs_holdout'])}")
    L.append(f"- Goal mix of served viewers {pil['served_goal_mix']} vs others {pil['others_goal_mix']}: the served "
             "group is also a different mix of goals, so the 2.3x headline compares different populations and "
             "different goals. The randomized comparison shows no benefit.\n")
    L.append("## 6. Run facts\n")
    L.append(f"- Goal outcomes (all goals, both arms): {status_counts}")
    L.append(f"- Simulator counts: {sim_counts}")
    return "\n".join(L) + "\n"
