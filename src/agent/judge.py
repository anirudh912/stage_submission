"""Close the loop: judge every goal (both arms) from attributable signals and record what was learned.

Signals used: the outcome stream only (simulator output: what the viewers did after the replay, plus the replay-day
events). Model output, our own action log and our expectations are never success signals.

Per goal:
  status       succeeded | failed | inconclusive
                 succeeded:    the CURRENT success rule (incl. corrected versions) is met inside the window
                 failed:       the whole window was observed and the rule was not met
                 inconclusive: the window runs past the end of observation, or the goal was abandoned because the
                               viewer's situation changed (nothing measurable left to judge)
  attribution  holdout | not_served | success_after_exposure | success_without_exposure |
               failed_after_exposure | failed_without_exposure | inconclusive
                 exposure = a personalised page of ours was SHOWN (home_page_shown with our action id), or a
                 message of ours was DELIVERED. Publishing alone is not exposure.

Attribution is descriptive for one viewer. Causal impact is only claimed in impact.py, by comparing the randomized
arms, never by comparing exposed with unexposed viewers.
"""
from collections import defaultdict
from datetime import timedelta

from .goals import success_event
from .timeutil import IST, iso, ts

COOLDOWN_DAYS = 7


def observation_end(trigger_days_ist, horizon_days=14):
    """End of the simulator horizon: midnight IST after the replay day + horizon days."""
    from datetime import datetime
    day = min(trigger_days_ist)
    start = datetime.fromisoformat(day).replace(tzinfo=IST)
    return start + timedelta(days=1 + horizon_days)


def index_outcomes(events):
    by_user = defaultdict(list)
    seen = set()
    for e in sorted(events, key=lambda x: x["received_at"]):
        if e["event_id"] in seen:            # same rule as everywhere: one event per event_id
            continue
        seen.add(e["event_id"])
        by_user[e["user_id"]].append({**e, "occurred_at_est": e["received_at"], "properties": e.get("properties") or {}})
    return by_user


def exposure(goal, actions, events):
    """When (if ever) the viewer actually experienced one of our actions for this goal."""
    mine = {a["action_id"]: a for a in actions if a.get("goal_instance") == goal["goal_instance_id"]}
    out = {"published": sorted(a["type"] for a in mine.values()), "page_shown": [], "message": {}}
    for e in events:
        p = e["properties"]
        if e["event"] == "home_page_shown" and p.get("personalised") and p.get("page_action_id") in mine:
            out["page_shown"].append(e["received_at"])
        elif e["event"] in ("message_delivered", "message_failed", "message_clicked") and p.get("action_id") in mine:
            out["message"].setdefault(p["action_id"], []).append(
                e["event"].replace("message_", "") + (f":{p['reason']}" if p.get("reason") else ""))
    firsts = out["page_shown"][:1] + [e["received_at"] for e in events if e["event"] == "message_delivered"
                                      and e["properties"].get("action_id") in mine][:1]
    out["first_exposure_at"] = min(firsts) if firsts else None
    out["page_shown"] = {"count": len(out["page_shown"]), "first": out["page_shown"][0] if out["page_shown"] else None}
    return out


def judge_goal(goal, arm, actions, events, pack, obs_end):
    deadline = ts(goal["deadline"])
    hit = success_event(goal, events, pack)
    exp = exposure(goal, actions, events)
    if hit:
        status, why = "succeeded", f"{hit['event']} {hit['event_id']} at {hit['received_at']}"
    elif goal["status"] == "abandoned":
        status, why = "inconclusive", f"abandoned during the window: {(goal.get('judgement') or {}).get('why')}"
    elif deadline > obs_end:
        status, why = "inconclusive", f"window ends {goal['deadline']}, after observation ends {iso(obs_end)}"
    else:
        status, why = "failed", f"no qualifying {goal['success_spec'].get('event')} by {goal['deadline']}"
    exposed = exp["first_exposure_at"] is not None
    if arm != "agent":
        attribution = "holdout"
    elif not exp["published"]:
        attribution = "not_served"
    elif status == "inconclusive":
        attribution = "inconclusive"
    elif status == "succeeded":
        attribution = "success_after_exposure" if exposed and exp["first_exposure_at"] <= hit["received_at"] \
            else "success_without_exposure"
    else:
        attribution = "failed_after_exposure" if exposed else "failed_without_exposure"
    return {
        "goal_instance_id": goal["goal_instance_id"], "viewer_id": goal["user_id"], "arm": arm,
        "goal_id": goal["goal_id"], "rule_version": goal["rule_version"],
        "success_rule": {k: goal["success_spec"].get(k) for k in ("event", "condition", "window_hours")},
        "started_at": goal["started_at"], "deadline": goal["deadline"], "status": status, "reason": why,
        "signal": hit and {"event_id": hit["event_id"], "event": hit["event"], "at": hit["received_at"]},
        "attribution": attribution, "exposure": exp,
        "provisional_status_during_replay": goal["status"] if goal["status"] != "open" else None,
        "rule_history": [h for h in goal.get("history", []) if h["change"] == "success_rule_corrected"],
    }


def lesson(o):
    """What the agent records for the viewer, and what it changes next time."""
    effects, notes = {}, []
    msgs = [s for v in o["exposure"]["message"].values() for s in v]
    if any(s.startswith("failed:token_invalid") for s in msgs):
        effects["push_unreachable"] = True
        notes.append("push token is invalid: do not plan push for this viewer")
    if o["attribution"] == "failed_after_exposure":
        effects["avoid_goal"] = {"goal_id": o["goal_id"], "until": iso(ts(o["deadline"]) + timedelta(days=COOLDOWN_DAYS))}
        notes.append(f"{o['goal_id']} failed although the viewer saw our action: try a different goal for "
                     f"{COOLDOWN_DAYS} days")
    elif o["attribution"] == "success_after_exposure":
        notes.append(f"{o['goal_id']} reached after exposure ({', '.join(o['exposure']['published'])})")
    elif o["attribution"] == "success_without_exposure":
        notes.append(f"{o['goal_id']} reached without seeing our action: do not credit the agent")
    elif o["attribution"] == "not_served":
        notes.append(f"{o['goal_id']} had no action published; outcome {o['status']}")
    if o["exposure"]["page_shown"]["count"] == 0 and "home_page" in o["exposure"]["published"]:
        notes.append("page was published but never shown (viewer did not reach home while it was live)")
    return {"goal_instance_id": o["goal_instance_id"], "goal_id": o["goal_id"], "result": o["status"],
            "attribution": o["attribution"], "notes": notes, "effects": effects}


def judge_records(store, pack, outcome_events, obs_end, no_actions=False):
    """Judge every goal in the store without writing anything. `no_actions=True` judges as if nothing had been
    published (used for the A/A check against a simulator run with an empty action file)."""
    by_user = index_outcomes(outcome_events)
    goal_rows = [store._goal(r) for r in store.db.execute("SELECT * FROM goals ORDER BY started_at")]
    return [judge_goal(g, store.arm(g["user_id"]), [] if no_actions else store.actions_for(g["user_id"]),
                       by_user.get(g["user_id"], []), pack, obs_end) for g in goal_rows]


def judge_all(store, pack, outcome_events, obs_end, judged_at):
    """Judge every goal, write the final judgement and a learning per goal. Returns the outcome records."""
    results = judge_records(store, pack, outcome_events, obs_end)
    for o in results:
        store.db.execute("UPDATE goals SET status=?, closed_at=?, judgement=? WHERE goal_instance_id=?",
                         (o["status"], judged_at, store_json(o), o["goal_instance_id"]))
        store.add_learning(o["viewer_id"], o["goal_instance_id"], judged_at, lesson(o))
    store.db.commit()
    return results


def store_json(o):
    import json
    return json.dumps({k: o[k] for k in ("status", "reason", "signal", "attribution", "rule_version")},
                      sort_keys=True)


def feedback(store, user_id, now):
    """Effects of past lessons that still apply at `now`: goals to avoid and channels to skip."""
    avoid, push_ok = set(), True
    for lr in store.learnings_for(user_id):
        eff = lr["note"].get("effects") or {}
        if eff.get("push_unreachable"):
            push_ok = False
        a = eff.get("avoid_goal")
        if a and ts(a["until"]) > ts(now):
            avoid.add(a["goal_id"])
    return {"avoid_goals": sorted(avoid), "push_ok": push_ok}
