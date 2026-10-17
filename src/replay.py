#!/usr/bin/env python3
"""Replay the trigger script through the agent (standard library only).

    python3 src/replay.py --pack ../data_pack/one_user_one_agent_pack --out sample_run

Feeds replay/triggers.jsonl, live_events.jsonl and control_events.jsonl in delivery order. Runs faster than real time
on a virtual clock: each decision's latency = queue wait + measured processing time + time spent waiting on
(simulated) dependencies, and published_at = deliver_at + that latency.

The harness injects the pack's surprises: model faults at the named triggers (mock provider), the model provider
outage and the data-store degradation for their windows. The agent is not told about them; it sees failing calls.
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.agent import LATENCY_FAIL_S, LATENCY_TARGET_S, Agent  # noqa: E402
from agent.bootstrap import build_store  # noqa: E402
from agent.deps import Windows  # noqa: E402
from agent.goals import GoalRegistry  # noqa: E402
from agent import impact  # noqa: E402
from agent.judge import judge_all, judge_records, observation_end  # noqa: E402
from agent.model import MockProvider  # noqa: E402
from agent.pack import Pack  # noqa: E402
from agent.timeutil import IST, iso, ts  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def read_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))], 1)


def ensure_prepared(pack, prepared):
    if not os.path.exists(os.path.join(prepared, "triggers_annotated.jsonl")):
        print(f"[replay] {prepared}/ missing: running prep_data.py", file=sys.stderr)
        subprocess.run([sys.executable, os.path.join(HERE, "prep_data.py"), "--pack", pack, "--out", prepared],
                       check=True, stdout=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", default=os.path.join(REPO, "..", "data_pack", "one_user_one_agent_pack"))
    ap.add_argument("--prepared", default=os.path.join(REPO, "prepared"))
    ap.add_argument("--out", default=os.path.join(REPO, "sample_run"))
    ap.add_argument("--state", default=os.path.join(REPO, "state", "replay.sqlite"))
    ap.add_argument("--workers", type=int, default=4, help="concurrent decision workers on the virtual clock")
    ap.add_argument("--max-model-calls", type=int, default=8)
    ap.add_argument("--max-tool-calls", type=int, default=20)
    ap.add_argument("--no-simulate", action="store_true", help="stop after the replay (no simulator/judge/impact)")
    ap.add_argument("--seeds", default="oua-practice,seed-2,seed-3,seed-4,seed-5",
                    help="simulator seeds; the first is the main run, the rest show simulator noise")
    a = ap.parse_args()
    pack_dir = os.path.abspath(a.pack)
    ensure_prepared(pack_dir, a.prepared)
    os.makedirs(os.path.dirname(a.state), exist_ok=True)
    os.makedirs(os.path.join(a.out, "viewer_state"), exist_ok=True)

    store = build_store(a.state, pack_dir, a.prepared)
    pack = Pack(pack_dir, a.prepared)
    registry = GoalRegistry(pack.goals_config)
    R = lambda name: os.path.join(pack_dir, "replay", name)
    controls = read_jsonl(R("control_events.jsonl"))

    # harness-side fault injection
    model_down, store_slow = Windows(), Windows()
    for c in controls:
        if c["type"] == "dependency_unavailable" and c.get("dependency") == "model_provider":
            model_down.add(c["at"], c["until"])
        if c["type"] == "dependency_degraded" and c.get("dependency") == "user_data_store":
            p95 = re.search(r"p95[^0-9]*([0-9.]+)\s*s", c.get("detail", ""))
            rate = re.search(r"([0-9.]+)%[^.]*time out", c.get("detail", ""))
            store_slow.add(c["at"], c["until"], {"p95_s": float(p95[1]) if p95 else 8.0,
                                                  "timeout_rate": float(rate[1]) / 100 if rate else 0.2})
    provider = MockProvider(read_jsonl(R("model_faults.jsonl")), outages=model_down)
    agent = Agent(store, pack, registry, provider, degraded=store_slow,
                  max_model_calls=a.max_model_calls, max_tool_calls=a.max_tool_calls)

    stream = [(c["at"], 0, "control", c) for c in controls]
    stream += [(e["deliver_at"], 1, "live", e) for e in read_jsonl(R("live_events.jsonl"))]
    stream += [(t["deliver_at"], 2, "trigger", t) for t in read_jsonl(os.path.join(a.prepared,
                                                                               "triggers_annotated.jsonl"))]
    stream.sort(key=lambda x: (x[0], x[1]))

    free = [0.0] * a.workers           # virtual time (epoch s) at which each worker is free
    decisions, duplicates, revisions = [], 0, []
    for at, _, kind, x in stream:
        if kind == "control":
            agent.handle_control(x)
        elif kind == "live":
            r = agent.handle_live_event(x)
            if r:
                revisions.append(r)
        else:
            w = min(range(a.workers), key=lambda i: free[i])
            deliver_s = ts(x["deliver_at"]).timestamp()
            start_s = max(deliver_s, free[w])
            rec = agent.handle_trigger(x, iso(ts(x["deliver_at"]).fromtimestamp(start_s, ts(x["deliver_at"]).tzinfo)),
                                       queue_wait_s=start_s - deliver_s)
            if rec is None:
                duplicates += 1
                continue
            free[w] = start_s + rec["decided_in_ms"] / 1000
            decisions.append(rec)

    # ---------------------------------------------------------------- outputs
    all_decisions = [r for r in (json.loads(x["record"]) for x in store.db.execute(
        "SELECT record FROM decisions ORDER BY created_at, decision_id"))]
    write_jsonl(os.path.join(a.out, "decisions.jsonl"), all_decisions)
    acts = store.all_actions()
    write_jsonl(os.path.join(a.out, "actions.jsonl"), [x for x in acts if not x.get("cancelled")])
    write_jsonl(os.path.join(a.out, "actions_cancelled.jsonl"), [x for x in acts if x.get("cancelled")])

    lat = [d["latency_ms"] for d in decisions]
    pub = [d for d in decisions if d["status"] == "PUBLISHED"]
    spike = [d for d in decisions if "2026-10-01T15:00" <= d["trigger"]["deliver_at"] < "2026-10-01T15:10"]
    by_hour = Counter(ts(d["trigger"]["deliver_at"]).astimezone(IST).strftime("%H") for d in decisions)
    in_window = lambda win: [d for d in decisions if win(d["trigger"]["deliver_at"])]
    report = {
        "decisions": len(decisions), "duplicate_deliveries_ignored": duplicates,
        "published": len(pub), "status": dict(Counter(d["status"] for d in decisions)),
        "latency_ms": {"p50": pct(lat, 50), "p95": pct(lat, 95), "p99": pct(lat, 99), "max": max(lat)},
        "processing_ms": {"p50": pct([d["processing_ms"] for d in decisions], 50),
                          "p99": pct([d["processing_ms"] for d in decisions], 99)},
        "over_target_60s": sum(d["latency_ms"] > LATENCY_TARGET_S * 1000 for d in decisions),
        "over_failure_300s": sum(d["latency_ms"] > LATENCY_FAIL_S * 1000 for d in decisions),
        "published_over_60s": sum(d["latency_ms"] > LATENCY_TARGET_S * 1000 for d in pub),
        "model": dict(Counter(d["model"].get("outcome", "not_called") for d in decisions)),
        "max_model_calls_in_a_decision": max(d["budget"]["model_calls"] for d in decisions),
        "max_tool_calls_in_a_decision": max(d["budget"]["tool_calls"] for d in decisions),
        "budgets": {"model_calls": a.max_model_calls, "tool_calls": a.max_tool_calls},
        "windows": {
            "model_outage": {"decisions": len(in_window(model_down)),
                             "outcomes": dict(Counter(d["model"].get("outcome") for d in in_window(model_down)))},
            "data_store_degraded": {
                "decisions": len(in_window(store_slow)),
                "status": dict(Counter(d["status"] for d in in_window(store_slow))),
                "latency_ms_p95": pct([d["latency_ms"] for d in in_window(store_slow)], 95),
                "latency_ms_max": max([d["latency_ms"] for d in in_window(store_slow)] or [0])},
            "evening_spike_15:00-15:10Z": {"decisions": len(spike),
                                           "latency_ms_p99": pct([d["latency_ms"] for d in spike], 99),
                                           "max_queue_wait_ms": max([d["queue_wait_ms"] for d in spike] or [0])},
        },
        "decisions_per_ist_hour": dict(sorted(by_hour.items())),
        "workers": a.workers,
        "note": "virtual clock: latency = queue wait + measured processing + simulated dependency waits",
    }
    with open(os.path.join(a.out, "latency_report.json"), "w") as f:
        json.dump(report, f, indent=1)
    with open(os.path.join(a.out, "run_log.json"), "w") as f:
        json.dump({"control_events": agent.log, "revisions": revisions,
                   "actions": {"published": len(acts) - sum(1 for x in acts if x.get("cancelled")),
                               "cancelled": sum(1 for x in acts if x.get("cancelled")),
                               "by_type": dict(Counter(x["type"] for x in acts if not x.get("cancelled")))}},
                  f, indent=1)
    summary = {k: report[k] for k in ("decisions", "duplicate_deliveries_ignored", "status", "latency_ms",
                                       "over_target_60s", "model")}

    if not a.no_simulate:
        summary["impact"] = measure(a, pack_dir, pack, store, all_decisions, registry)

    vs = os.path.join(a.out, "viewer_state")
    for f in os.listdir(vs):
        if f.endswith(".json"):
            os.remove(os.path.join(vs, f))
    for uid in sorted({d["viewer_id"] for d in all_decisions}):
        with open(os.path.join(vs, f"{uid}.json"), "w") as f:
            json.dump(store.viewer_snapshot(uid), f, indent=1, ensure_ascii=False)
    print(json.dumps(summary, indent=1))
    print(f"[replay] wrote {a.out}/decisions.jsonl, actions.jsonl, viewer_state/, latency_report.json, run_log.json",
          file=sys.stderr)


def simulate(pack_dir, actions_path, out_dir, seed):
    subprocess.run([sys.executable, "-I", os.path.join(pack_dir, "simulator", "simulate_outcomes.py"),
                    "--pack", pack_dir, "--actions", actions_path, "--out", out_dir, "--seed", seed],
                   check=True, stderr=subprocess.DEVNULL)
    return read_jsonl(os.path.join(out_dir, "outcome_events.jsonl")), \
        json.load(open(os.path.join(out_dir, "simulator_report.json")))


def measure(a, pack_dir, pack, store, decisions, registry):
    """Simulate, judge every goal (both arms), write outcomes.jsonl and the impact report."""
    seeds = [x for x in a.seeds.split(",") if x]
    actions_path = os.path.join(a.out, "actions.jsonl")
    sim_dir = os.path.join(a.out, "sim")
    trig_days = {ts(d["trigger"]["deliver_at"]).astimezone(IST).date().isoformat()
                 for d in decisions if d.get("trigger", {}).get("deliver_at")}
    obs_end = observation_end(trig_days)
    events, sim_rep = simulate(pack_dir, actions_path, sim_dir, seeds[0])
    if sim_rep["policy_problem_count"]:
        print(f"[replay] WARNING: simulator reports {sim_rep['policy_problem_count']} policy problems",
              file=sys.stderr)
    judged_at = iso(obs_end)
    outcomes = judge_all(store, pack, events, obs_end, judged_at)
    write_jsonl(os.path.join(a.out, "outcomes.jsonl"), outcomes)

    main = impact.itt(outcomes)
    effects = [main["diff_pts"]]
    tmp = os.path.join(a.out, "sim", "_seeds")
    for seed in seeds[1:]:
        ev_s, _ = simulate(pack_dir, actions_path, tmp, seed)
        effects.append(impact.itt(judge_records(store, pack, ev_s, obs_end))["diff_pts"])
    empty = os.path.join(tmp, "empty_actions.jsonl")
    os.makedirs(tmp, exist_ok=True)
    open(empty, "w").close()
    ev_aa, _ = simulate(pack_dir, empty, tmp, seeds[0])
    fit = impact.goal_fit(decisions, events, ev_aa, store, pack, registry, obs_end)
    aa_records = judge_records(store, pack, ev_aa, obs_end, no_actions=True)
    aa = impact.itt(aa_records)
    truth = impact.ground_truth(outcomes, aa_records)
    cov = impact.pre_covariates(store)
    adj, adj_aa = impact.adjusted(outcomes, cov), impact.adjusted(aa_records, cov)
    for f in os.listdir(tmp):
        os.remove(os.path.join(tmp, f))
    os.rmdir(tmp)

    arms = {r["user_id"]: r["arm"] for r in csv.DictReader(open(os.path.join(pack_dir, "replay", "arms.csv")))}
    triggered = {d["viewer_id"] for d in decisions if d.get("kind") is None}
    beh = impact.behaviour(events, arms, triggered)
    mis = impact.misleading(outcomes)
    pil = impact.pilot(pack_dir)
    goals = impact.by_goal(outcomes)
    exp_rate = impact.exposure_rate(outcomes)
    status_counts = dict(Counter(o["status"] for o in outcomes))
    seeds_info = {"seeds": seeds, "effects": effects, "main_drop_inconclusive": impact.itt(outcomes, True)}
    with open(os.path.join(a.out, "impact.json"), "w") as f:
        json.dump({"itt_first_goal": main, "seeds": seeds_info, "aa_check_no_actions": aa,
                   "simulator_ground_truth": truth, "adjusted_estimates": adj,
                   "adjusted_estimates_aa": adj_aa, "goal_fit": fit, "by_goal": goals,
                   "behaviour_14d": beh, "misleading_comparisons": mis, "pilot": pil,
                   "agent_arm_exposure_rate": exp_rate, "goal_status_counts": status_counts,
                   "simulator_counts": sim_rep["counts"], "simulator_policy_problems": sim_rep["policy_problem_count"],
                   "observation_end": iso(obs_end)}, f, indent=1)
    with open(os.path.join(a.out, "impact_report.md"), "w") as f:
        f.write(impact.report_md(main, seeds_info, aa, beh, mis, pil, goals, exp_rate, status_counts,
                                 sim_rep["counts"], truth, adj, adj_aa, fit))
    return {"itt_diff_pts": main["diff_pts"], "ci95_pts": main["ci95_pts"], "p_value": main["p_value"],
            "seed_effects": effects, "aa_diff_pts": aa["diff_pts"],
            "true_effect_pts": truth["agent"]["true_effect_pts"],
            "adjusted": {k: adj[k]["diff_pts"] for k in ("cuped", "stratified_by_goal", "cuped_and_stratified")},
            "adjusted_ci": {k: adj[k]["ci95_pts"] for k in ("cuped", "stratified_by_goal", "cuped_and_stratified")},
            "goal_status": status_counts,
            "simulator_policy_problems": sim_rep["policy_problem_count"]}


if __name__ == "__main__":
    main()
