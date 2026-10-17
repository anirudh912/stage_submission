"""The per-viewer agent loop: understand -> choose goal -> (advisor) -> plan -> verify -> act -> record.

One Agent object serves every viewer, but all state is per viewer in the store, and the database enforces one open
goal per viewer, so no two code paths can pursue different goals for the same viewer.

Holdout viewers run through the exact same path (same goal logic, same advisor call), so their "shadow" goals are
comparable; only the final publish step is skipped. This is what makes the holdout comparison fair.
"""
import time
from datetime import timedelta

from .actions import PAGE_TTL, STALE_TRIGGER_S, Catalog, choose_actionable_goal, fallback_page, live_page, plan
from .deps import DataStoreTimeout, StoreProxy
from .goals import PRIORITY, apply_rule_correction, eligibility, success_event
from .judge import feedback
from .model import CircuitBreaker, ModelError, build_request, parse_advice
from .pack import CATALOGUE_DIALECTS
from .resolver import resolve
from .timeutil import age_s, iso, ts
from .verifier import Verifier

MODEL_TIMEOUT_S = 5.0
LATENCY_TARGET_S, LATENCY_FAIL_S = 60, 300
OVERLOAD_SKIP_MODEL_S = 15       # if a trigger waited this long in the queue, skip the advisor (fast path)
STATUS_GOALS = {"G04", "G05", "G06", "G08", "G09"}
DECISION_CHECKS = {"arm", "duplicate_trigger", "evidence", "eligibility", "single_goal", "budget"}


class Agent:
    def __init__(self, store, pack, registry, provider, degraded=None, max_model_calls=8, max_tool_calls=20):
        self.store, self.pack, self.registry, self.provider = store, pack, registry, provider
        self.catalog = Catalog(pack)
        self.io = StoreProxy(store, degraded, max_tool_calls)
        self.verifier = Verifier(self.io, self.catalog, pack, registry, max_model_calls, max_tool_calls)
        self.breaker = CircuitBreaker()
        self.max_model_calls = max_model_calls
        self.n_decisions = self.n_actions = 0
        self.log = []                  # control / revision log lines for the run report
        self.pages_sent = {}           # user_id -> (published_at, dialect) of the last page: in-process memory, so
                                       # the degraded-store fallback never needs a store read to avoid a live page

    # ------------------------------------------------------------------ ids
    def _did(self, prefix="D"):
        self.n_decisions += 1
        return f"{prefix}-{self.n_decisions:05d}"

    def _aid(self):
        self.n_actions += 1
        return f"A-{self.n_actions:05d}"

    # ------------------------------------------------------------------ trigger
    def handle_trigger(self, trig, start_at, queue_wait_s=0.0):
        """Decide for one app-open/login trigger. Returns the decision record, or None for a duplicate delivery."""
        if self.store.decision_for_trigger(trig["event_id"]):
            self.store.ingest_event(trig, "trigger")            # counts the redelivery, no new decision
            return None
        t0 = time.perf_counter()
        uid, deliver = trig["user_id"], trig["deliver_at"]
        now = iso(ts(start_at))
        did = self._did()
        self.store.ingest_event(trig, "trigger")
        self.io.begin(did, now)
        rec = {"decision_id": did, "viewer_id": uid, "created_at": now,
               "trigger": {"event": trig["event"], "event_id": trig["event_id"], "received_at": trig["received_at"],
                           "deliver_at": deliver,
                           "event_age_s": age_s((trig.get("_q") or {}).get("occurred_at_est") or trig["received_at"],
                                                deliver),
                           "copies_seen": self.store.event_copies(trig["event_id"])},
               "queue_wait_ms": round(queue_wait_s * 1000, 1)}
        model_info = {"called": False}
        try:
            arm = self.io.arm(uid)
            rec["arm"] = arm
            view = resolve(self.io, self.pack, uid, now, trig)
            og = self._close_if_due(self.io.open_goal(uid), view, now)
            fb = feedback(self.io, uid, now)
            choice = choose_actionable_goal(view, self.registry, self.pack, self.catalog, now, open_goal=og,
                                            avoid=fb["avoid_goals"])
            model_info = self._advise(trig, view, choice, now, queue_wait_s)
            p = plan(view, choice, self.pack, self.catalog, self.io, now, arm, push_ok=fb["push_ok"])
            gd = {"goal_id": choice.goal_id, "evidence": choice.evidence, "success": choice.success,
                  "continuing": choice.continuing}
            approved, checks = self.verifier.verify(
                uid, trig, now, gd, p["actions"], open_goal=og,
                budget={"model_calls": model_info.get("calls", 0), "tool_calls": self.io.tool_calls})
        except DataStoreTimeout as e:
            return self._degraded(rec, trig, now, t0, deliver, queue_wait_s, model_info, e)

        decision_ok = not any(f["check"] in DECISION_CHECKS for f in checks["failed"])
        goal_instance = choice.continuing
        if choice.goal_id and decision_ok and not choice.continuing:
            goal_instance = f"GI-{did[2:]}"
            self.store.create_goal({"goal_instance_id": goal_instance, "user_id": uid, "goal_id": choice.goal_id,
                                    "started_at": now, "deadline": choice.deadline, "success_spec": choice.success,
                                    "rule_version": choice.success["rule_version"], "decision_id": did,
                                    "evidence": choice.evidence})
        conf, conf_reason = choice.confidence, choice.confidence_reason
        if model_info.get("outcome") == "disagreed":
            conf, conf_reason = round(max(conf - 0.1, 0.05), 2), conf_reason + "; advisor suggested another goal"
        dialect = view.f("dialect")
        rec.update({
            "goal": {"id": choice.goal_id, "instance": goal_instance, "deadline": choice.deadline,
                     "success_signal": choice.success and {k: choice.success[k] for k in
                                                           ("event", "condition", "window_hours", "rule_version")
                                                           if k in choice.success},
                     "target": (choice.success or {}).get("target"),
                     "continuing": bool(choice.continuing), "why": choice.reason,
                     "shadow": arm != "agent", "considered": choice.considered},
            "dialect": {"used": dialect.value, "source": dialect.source, "rule": dialect.rule,
                        "conflicts": [f"{c['source']}:{c['value']}" for c in dialect.conflicts]},
            "content": p["content"] if approved else [],
            "rows_explained": p["rows_explained"] if approved else [],
            "confidence": conf, "confidence_reason": conf_reason,
            "evidence": self._evidence(choice.evidence + p["evidence"], view, now),
            "assumptions": p["assumptions"],
            "conflicts": view.conflicts(),
            "lessons_applied": fb,
            "checks": checks, "model": model_info,
        })
        self.store.record_facts(uid, did, view.fact_list(), now)
        if not decision_ok:
            bad = [f for f in checks["failed"] if f["check"] in DECISION_CHECKS]
            return self._finish(rec, t0, deliver, queue_wait_s, model_info, status="REJECTED",
                                reason="verifier rejected the decision: " +
                                       "; ".join(f"{f['check']}: {f['detail']}" for f in bad))
        if not approved:
            reason = p["no_action_reason"] or "verifier dropped every proposed action: " + \
                "; ".join(f"{f['check']}: {f['detail']}" for f in checks["failed"])
            status = "SHADOW_NO_ACTION" if arm != "agent" else "NO_ACTION"
            return self._finish(rec, t0, deliver, queue_wait_s, model_info, status=status, reason=reason)
        return self._finish(rec, t0, deliver, queue_wait_s, model_info, status="PUBLISHED", actions=approved,
                            goal_instance=goal_instance, trigger_id=trig["event_id"])

    def _degraded(self, rec, trig, now, t0, deliver, queue_wait_s, model_info, err):
        """The data store failed mid-decision. Nothing personal can be trusted, so no goal and no message. If the
        arm was already read as 'agent', the trigger is fresh and the app dialect has content, publish a
        catalogue-only page (most-played in the app's dialect) rather than leave the default layout."""
        reason = f"user data store degraded: {err}; not acting on partial data"
        d = trig.get("app_dialect")
        last = self.pages_sent.get(trig["user_id"])
        why_not = ("arm unknown (not read before the store failed)" if rec.get("arm") is None else
                   f"arm is {rec['arm']!r}" if rec["arm"] != "agent" else
                   "trigger is stale" if (rec["trigger"]["event_age_s"] or 0) > STALE_TRIGGER_S else
                   f"app dialect {d!r} has no catalogue content" if d not in CATALOGUE_DIALECTS else
                   "a page we published for this dialect is still live"
                   if last and last[1] == d and ts(last[0]) + PAGE_TTL > ts(now) else None)
        page = None if why_not else fallback_page(self.catalog, d, trig.get("app_language"), now)
        if page:
            approved, checks = self.verifier.verify_fallback_page(page, trig, now)
            if approved:
                rec.update({
                    "goal": {"id": None, "why": "viewer data unavailable (data store degraded): no goal is opened "
                                                "on partial data"},
                    "dialect": {"used": d, "source": "app_setting", "rule": "R-dialect-1", "conflicts": []},
                    "content": [f"title:{t}" for t in [page.get("hero_title_id")] +
                                [t for r in page["rows"] for t in r["title_ids"]] if t],
                    "rows_explained": ([{"row_type": "hero", "why": page.pop("_hero_why")}] if page.get("_hero_why")
                                       else []) + [{"row_type": r["row_type"], "why": r.pop("why")}
                                                   for r in page["rows"]],
                    "checks": checks, "confidence": 0.5,
                    "confidence_reason": "catalogue only: nothing about this viewer could be read",
                    "fallback": {"why": reason},
                })
                return self._finish(rec, t0, deliver, queue_wait_s, model_info, status="DEGRADED_FALLBACK_PAGE",
                                    actions=[page], trigger_id=trig["event_id"])
            why_not = "fallback page failed checks: " + "; ".join(f["check"] for f in checks["failed"])
        rec["fallback"] = {"why_not": why_not or f"no content in {d}"}
        return self._finish(rec, t0, deliver, queue_wait_s, model_info, status="DEGRADED_NO_ACTION", reason=reason)

    def _finish(self, rec, t0, deliver, queue_wait_s, model_info, status, reason=None, actions=(),
                goal_instance=None, trigger_id=None):
        processing_s = time.perf_counter() - t0
        virtual_s = self.io.wait_s + model_info.get("elapsed_s", 0.0)
        decided_s = processing_s + virtual_s
        published = ts(deliver) + timedelta(seconds=queue_wait_s + decided_s)
        latency = queue_wait_s + decided_s
        if actions and latency > LATENCY_FAIL_S:
            status, reason, actions = "LATE_NO_ACTION", f"decision took {latency:.0f}s (> {LATENCY_FAIL_S}s)", ()
        published_ids = []
        for a in actions:
            out = {k: v for k, v in a.items() if k != "evidence"}
            out.update({"action_id": self._aid(), "decision_id": rec["decision_id"], "user_id": rec["viewer_id"],
                        "trigger_event_id": trigger_id, "published_at": iso(published),
                        "goal_instance": goal_instance})
            if out["type"] == "in_app_message":
                out["send_at"] = out["published_at"]       # queued when published, shown at the next app open
            self.store.save_action(out)
            published_ids.append(out["action_id"])
            if out["type"] == "home_page":
                self.pages_sent[out["user_id"]] = (out["published_at"], out["dialect"])
        types = [a["type"] for a in actions]
        rec.setdefault("model", model_info)
        rec.setdefault("goal", {"id": None, "why": "not chosen: decision stopped early"})
        rec.setdefault("checks", {"passed": [], "failed": []})
        rec.update({
            "action": "+".join(t.upper() for t in types) if types else "NONE",
            "action_ids": published_ids,
            "status": status, "no_action_reason": reason if not actions else None,
            "decided_in_ms": round(decided_s * 1000, 1),
            "processing_ms": round(processing_s * 1000, 2),
            "waited_on_dependencies_ms": round(virtual_s * 1000, 1),
            "latency_ms": round(latency * 1000, 1),
            "published_at": iso(published) if actions else None,
            "within_budget": latency <= LATENCY_TARGET_S,
            "budget": {"model_calls": model_info.get("calls", 0), "tool_calls": self.io.tool_calls,
                       "tool_retries": self.io.retries, "tool_timeouts": self.io.timeouts},
        })
        if rec.get("goal", {}).get("deadline"):
            rec["judged_by"] = {"deadline": rec["goal"]["deadline"], "signal": rec["goal"]["success_signal"]}
        self.store.save_decision(rec)
        return rec

    # ------------------------------------------------------------------ advisor
    def _advise(self, trig, view, choice, now, queue_wait_s):
        """One advisor call. Its output can only lower confidence or be rejected; it never adds evidence."""
        info = {"called": False, "provider": self.provider.name, "calls": 0, "elapsed_s": 0.0}
        now_s = ts(now).timestamp()
        if queue_wait_s > OVERLOAD_SKIP_MODEL_S:
            info["outcome"] = "skipped_overload"
            return info
        if not self.breaker.allow(now_s):
            info["outcome"] = "skipped_circuit_open"
            return info
        if self.max_model_calls < 1:
            info["outcome"] = "skipped_budget"
            return info
        info.update(called=True, calls=1)
        try:
            raw, elapsed = self.provider.advise_goal(build_request(trig, view, choice, PRIORITY), now, MODEL_TIMEOUT_S)
        except ModelError as e:
            self.breaker.record(False, now_s)
            info.update(outcome=e.kind, detail=e.detail, elapsed_s=e.elapsed_s, fallback="rules only")
            return info
        self.breaker.record(True, now_s)
        info["elapsed_s"] = elapsed
        advice, problem = parse_advice(raw)
        if problem:
            info.update(outcome="malformed", detail=problem, fallback="rules only")
            return info
        info["claim"] = advice
        gid = advice["goal_id"]
        if gid == choice.goal_id:
            info["outcome"] = "agreed"
            return info
        if choice.continuing:
            info.update(outcome="disagreed_open_goal_kept",
                        detail=f"advisor suggested {gid}; the open goal {choice.continuing} stays (one active plan)")
            return info
        # the claim is checked against data by the independent verifier, never accepted on its own say-so
        series = view.value("unfinished_series")
        here = [x for x in series if x["content_slug"].endswith("-" + str(view.value("dialect")))]
        ok, why = self.verifier.check_claim(view.user_id, trig, now, gid, target=(here or series or [None])[0])
        cons = {c["goal_id"]: c for c in choice.considered}
        rule = cons.get(gid, {})
        if gid and ok and rule.get("eligible") and not rule.get("skipped"):
            info.update(outcome="disagreed", detail=f"advisor prefers {gid}; rules keep {choice.goal_id} by priority")
        else:
            info.update(outcome="rejected_unsupported_claim",
                        detail=f"advisor claimed {gid} eligible (confidence {advice['confidence']}); "
                               f"data says: {why or rule.get('why')}. Model text is not evidence.")
        return info

    # ------------------------------------------------------------------ goal lifecycle
    def _close_if_due(self, og, view, now):
        """Close an open goal whose deadline has passed, from events the agent has received. Returns the goal
        if it is still open."""
        if not og or ts(og["deadline"]) > ts(now):
            return og
        hit = success_event(og, view.events, self.pack)
        status = "succeeded" if hit else "failed"
        self.store.close_goal(og["goal_instance_id"], status, now,
                              {"by": "deadline", "signal_event": hit and hit["event_id"], "provisional": True})
        return None

    def handle_live_event(self, e):
        """A non-trigger event arrives. Status changes may make the current plan obsolete."""
        self.store.ingest_event(e, "live")
        uid, now = e["user_id"], e["deliver_at"]
        status_change = e["event"] in ("purchase_completed", "subscription_renewed", "subscription_lapsed",
                                       "notification_opt_out")
        if not status_change:
            # any other event only matters if it could be the open goal's success signal
            og = self.store.open_goal(uid)
            if not og or og["success_spec"].get("event") != e["event"]:
                return None
        out = {"decision_id": self._did("R"), "viewer_id": uid, "created_at": now, "kind": "REVISION",
               "trigger": {"event": e["event"], "event_id": e["event_id"], "received_at": e["received_at"]},
               "changes": []}
        if e["event"] == "notification_opt_out":
            ch = e["properties"].get("channel")
            out["changes"] += self._cancel_pending(uid, now, lambda a: a["type"] == ch,
                                                   f"viewer opted out of {ch} ({e['event_id']})")
        og = self.store.open_goal(uid)
        if og and e["event"] != "notification_opt_out":
            self.io.begin(out["decision_id"], now)
            view = resolve(self.store, self.pack, uid, now)
            hit = success_event(og, view.events, self.pack)
            # Only goals that rest on subscription status can stop applying mid-window. A start condition such as
            # "no app open in 14 days" (G12) or "never played" (G01) is SUPPOSED to stop being true.
            elig = eligibility(view, self.registry, self.pack).get(og["goal_id"]) \
                if status_change and og["goal_id"] in STATUS_GOALS else None
            if hit:
                self.store.close_goal(og["goal_instance_id"], "succeeded", now,
                                      {"by": "success_event", "signal_event": hit["event_id"], "provisional": True})
                out["changes"].append(f"goal {og['goal_instance_id']} ({og['goal_id']}) success observed: "
                                      f"event:{hit['event_id']}")
            elif elig is not None and not elig.eligible:
                self.store.close_goal(og["goal_instance_id"], "abandoned", now,
                                      {"by": "situation_changed", "why": elig.reasons[0], "cause": e["event_id"]})
                out["changes"].append(f"goal {og['goal_instance_id']} ({og['goal_id']}) no longer applies: "
                                      f"{elig.reasons[0]}")
            if hit or (elig is not None and not elig.eligible):
                out["changes"] += self._cancel_pending(
                    uid, now, lambda a: a.get("goal_instance") == og["goal_instance_id"] and a["type"] != "home_page",
                    f"goal {og['goal_instance_id']} closed after {e['event']}")
        if not out["changes"]:
            return None
        self.store.save_decision({**out, "status": "REVISED"})
        return out

    def _pending(self, a, now):
        """Not yet seen by the viewer. A push or WhatsApp is pending until its send time; an in-app message until the
        viewer's next app open after it was queued."""
        if a["type"] != "in_app_message":
            return ts(a.get("send_at") or a["published_at"]) > ts(now)
        return not any(e["event"] in ("app_open", "login_success") and e["received_at"] > a["published_at"]
                       for e in self.store.events_for(a["user_id"], until=now))

    def _cancel_pending(self, uid, now, match, reason):
        done = []
        for a in self.store.actions_for(uid):
            if a["type"] in ("push", "whatsapp", "in_app_message") and not a.get("cancelled") and match(a) \
                    and self._pending(a, now):
                self.store.cancel_action(a["action_id"], now, reason)
                done.append(f"cancelled {a['type']} {a['action_id']} (was due {a.get('send_at')}): {reason}")
        return done

    # ------------------------------------------------------------------ control events
    def handle_control(self, c):
        kind = c["type"]
        if kind == "title_withdrawn":
            return self._withdraw(c)
        if kind == "goal_rule_corrected":
            affected = apply_rule_correction(self.store, self.registry, c)
            entry = {"at": c["at"], "type": kind, "goal_id": c["goal_id"], "new_rule_version":
                     self.registry.versions[c["goal_id"]], "affected_open_goals": affected,
                     "note": "only open goals of this type were re-pointed; closed goals keep the rule they were "
                             "judged under; no other viewer is reprocessed"}
            self.log.append(entry)
            return entry
        entry = {"at": c["at"], "type": kind, "dependency": c.get("dependency"), "until": c.get("until"),
                 "note": "injected by the harness; the agent sees it only as failing or slow calls"}
        self.log.append(entry)
        return entry

    def _withdraw(self, c):
        tid, at = c["title_id"], c["at"]
        self.catalog.withdraw(tid, at)
        start = iso(ts(at) - PAGE_TTL)
        repaired, cancelled = [], []
        for pg in self.store.pages_published_between(start, at):
            ids = ([pg.get("hero_title_id")] if pg.get("hero_title_id") else []) + \
                  [t for r in pg["rows"] for t in r["title_ids"]]
            if tid not in ids or live_page(self.store, pg["user_id"], at)["action_id"] != pg["action_id"]:
                continue
            fix = {k: v for k, v in pg.items() if k not in ("action_id", "published_at", "decision_id")}
            if fix.get("hero_title_id") == tid:
                fix.pop("hero_title_id")
            fix["rows"] = [{**r, "title_ids": [t for t in r["title_ids"] if t != tid]} for r in pg["rows"]]
            fix["rows"] = [r for r in fix["rows"] if len(r["title_ids"]) >= 3]
            did = self._did("R")
            rec = {"decision_id": did, "viewer_id": pg["user_id"], "created_at": at, "kind": "REPAIR",
                   "trigger": {"event": "title_withdrawn", "title_id": tid, "at": at}, "replaces": pg["action_id"]}
            if fix["rows"] or fix.get("hero_title_id"):
                fix.update({"action_id": self._aid(), "decision_id": did, "published_at": at,
                            "repair_of": pg["action_id"]})
                self.store.save_action(fix)
                rec.update(status="PUBLISHED", action="HOME_PAGE", action_ids=[fix["action_id"]])
                repaired.append(pg["user_id"])
            else:
                rec.update(status="NO_ACTION", no_action_reason="nothing valid left on the page after removing "
                                                                f"{tid}; the platform drops it")
            self.store.save_decision(rec)
        for a in self.store.all_actions():
            if a.get("title_id") == tid and a["type"] in ("push", "whatsapp", "in_app_message") \
                    and not a.get("cancelled") and self._pending(a, at):
                self.store.cancel_action(a["action_id"], at, f"{tid} withdrawn: {c.get('reason')}")
                cancelled.append(a["action_id"])
        entry = {"at": at, "type": "title_withdrawn", "title_id": tid, "pages_republished_for": repaired,
                 "messages_cancelled": cancelled}
        self.log.append(entry)
        return entry

    # ------------------------------------------------------------------ evidence with freshness
    def _evidence(self, refs, view, now):
        by_id = {e["event_id"]: e for e in view.events}
        out = []
        for r in dict.fromkeys(refs):
            kind, _, val = r.partition(":")
            item = {"ref": r}
            if kind == "event" and val in by_id:
                item.update(event=by_id[val]["event"], received_at=by_id[val]["received_at"],
                            age_s=age_s(by_id[val]["received_at"], now))
            elif kind == "profile":
                item["age_s"] = age_s(val.split("@")[1], now)
            out.append(item)
        return out
