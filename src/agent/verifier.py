"""Independent verifier.

It does NOT reuse the resolver's facts or the goal picker's reasoning. It re-reads raw events and profile rows from
the store, the catalogue (with live withdrawals) and the config, and re-derives every condition with its own,
deliberately simple code. So it can disagree with the decision step, and it is the only thing that can approve an
action. Model output and generated text are never treated as evidence here.

Decision-level checks (a failure drops every action):
  arm, duplicate_trigger, evidence, eligibility, single_goal, budget
Action-level checks (a failure drops that action only):
  page_dialect, page_rules, availability, injection, duplicate_page,
  consent, quiet_hours, frequency_cap, message_title, copy, template
"""
import re
from datetime import timedelta

from .timeutil import IST, ist_day, ts

INJECTION = re.compile(r"(ignore (your|all|previous)|system note|assistant instruction|for ai agents|"
                       r"skip (all )?checks|every user|pre-approved)", re.I)
MAX_MODEL_CALLS, MAX_TOOL_CALLS = 8, 20
PAGE_TTL = timedelta(minutes=120)


class Verifier:
    def __init__(self, store, catalog, pack, registry, max_model_calls=MAX_MODEL_CALLS, max_tool_calls=MAX_TOOL_CALLS):
        self.store, self.catalog, self.pack, self.registry = store, catalog, pack, registry
        self.max_model_calls, self.max_tool_calls = max_model_calls, max_tool_calls
        iv = pack.interventions
        self.templates = {t["template_id"]: t for t in iv["whatsapp"]["approved_templates"]}
        self.forbidden = [p.lower() for p in iv["push"]["copy"]["forbidden_phrases"]]
        self.prices = {pl["price_inr"] for pl in pack.pricing["plans"]}
        self.quiet = iv["push"]["quiet_hours_ist"]           # ["22:00", "08:00"]

    # ------------------------------------------------------------------ entry point
    def verify(self, user_id, trigger, now, goal, actions, open_goal=None, budget=None):
        """goal: dict with goal_id, evidence, success(target) and continuing. Returns (approved_actions, checks)."""
        self.now = ts(now)
        self.events = self.store.events_for(user_id, until=self.now)
        self.profiles = self.store.profile_versions(user_id)
        passed, failed = [], []

        def check(name, ok, detail=None, idx=None):
            if ok:
                if name not in passed:
                    passed.append(name)
            else:
                failed.append({"check": name, "detail": detail, **({"action": idx} if idx is not None else {})})
            return ok

        decision_ok = True
        if actions:
            decision_ok &= check("arm", self.store.arm(user_id) == "agent",
                                 f"arm is {self.store.arm(user_id)!r}; only the agent arm may receive actions")
        if trigger is not None:
            prior = self.store.decision_for_trigger(trigger["event_id"])
            decision_ok &= check("duplicate_trigger", prior is None,
                                 f"trigger already decided in {prior and prior['decision_id']}")
        if goal and goal.get("goal_id"):
            ok, why = self._evidence(user_id, goal.get("evidence") or [])
            decision_ok &= check("evidence", ok, why)
            if not goal.get("continuing"):
                ok, why = self._eligible(user_id, goal, trigger)
                decision_ok &= check("eligibility", ok, why)
            og = open_goal
            decision_ok &= check("single_goal", og is None or og["goal_instance_id"] == goal.get("continuing"),
                                 f"viewer already owns open goal {og and og['goal_instance_id']} ({og and og['goal_id']})")
        elif actions:
            decision_ok &= check("eligibility", False, "actions proposed without a goal")
        if budget:
            decision_ok &= check("budget", budget.get("model_calls", 0) <= self.max_model_calls
                                 and budget.get("tool_calls", 0) <= self.max_tool_calls,
                                 f"used {budget.get('model_calls', 0)} model / {budget.get('tool_calls', 0)} tool "
                                 f"calls; limits {self.max_model_calls} / {self.max_tool_calls}")

        approved = []
        for i, a in enumerate(actions):
            results = (self._page(user_id, a, trigger) if a["type"] == "home_page"
                       else self._message(user_id, a) if a["type"] in ("push", "whatsapp")
                       else self._in_app(user_id, a) if a["type"] == "in_app_message"
                       else [("action_type", False, f"unsupported action type {a['type']}")])
            ok_all = True
            for name, ok, detail in results:
                ok_all &= check(name, ok, detail, i)
            if decision_ok and ok_all:
                approved.append(a)
        return approved, {"passed": passed, "failed": failed}

    def check_claim(self, user_id, trigger, now, goal_id, target=None):
        """Check an advisor's 'viewer is eligible for goal X' claim against the data only."""
        if not goal_id:
            return False, "no goal named"
        if goal_id not in self.registry.goals:
            return False, f"{goal_id} is not a goal in goals.json"
        self.now = ts(now)
        self.events = self.store.events_for(user_id, until=self.now)
        self.profiles = self.store.profile_versions(user_id)
        return self._eligible(user_id, {"goal_id": goal_id, "success": {"target": target}}, trigger)

    # ------------------------------------------------------------------ decision-level
    def _evidence(self, user_id, refs):
        """Every event/profile ref must exist, belong to this viewer and be known at decision time.
        At least one data ref (not just a rule ref) is required: a claim with no data behind it is rejected."""
        mine = {e["event_id"] for e in self.events}
        prof = {f"profile:{p['user_id']}@{p['updated_at']}" for p in self.profiles}
        data_refs, bad = 0, []
        for r in refs:
            kind, _, val = r.partition(":")
            if kind == "event":
                data_refs += 1
                if val not in mine:
                    bad.append(r)
            elif kind == "profile":
                data_refs += 1
                if r not in prof:
                    bad.append(r)
            elif kind == "catalogue":
                if val not in self.pack.titles and val not in self.pack.slugs:
                    bad.append(r)
            elif kind == "absence":
                data_refs += 1       # "no such event in N scanned": re-checked by eligibility below
            elif kind not in ("goal_rule", "pricing"):
                bad.append(r)
        if bad:
            return False, f"evidence not found for this viewer at decision time: {bad[:3]}"
        if data_refs == 0:
            return False, "no event or profile evidence supports the goal"
        return True, None

    def _signup(self):
        s = [e for e in self.events if e["event"] == "signup_completed"]
        if s:
            return ts(s[0]["occurred_at_est"])
        return ts(self.profiles[0]["signup_at"]) if self.profiles and self.profiles[0].get("signup_at") else None

    def _status_view(self):
        """(status or None if not determinable, all distinct values seen). Events after the newest row win."""
        newest = self.profiles[0]["updated_at"] if self.profiles else ""
        later = [e for e in self.events if e["event"] in ("purchase_completed", "subscription_renewed",
                                                           "subscription_lapsed") and e["received_at"] > newest]
        if later:
            e = later[-1]
            return ("lapsed" if e["event"] == "subscription_lapsed" else
                    "trial_active" if e["properties"].get("is_trial") else "active"), True
        vals = {p["subscription_status"] for p in self.profiles}
        return (vals.pop() if len(vals) == 1 else None), False

    def _eligible(self, user_id, goal, trigger):
        gid = goal["goal_id"]
        ev = lambda name: [e for e in self.events if e["event"] == name]
        status, from_event = self._status_view()
        paid_events = [e for e in self.events if e["event"] == "subscription_renewed" or
                       (e["event"] == "purchase_completed" and not e["properties"].get("is_trial"))]
        signup = self._signup()
        days_since = lambda t: (self.now - t).total_seconds() / 86400 if t else None
        if gid in ("G10", "G11"):
            return False, f"{gid} has no observable success signal"
        if gid == "G01":
            d = days_since(signup)
            return (d is not None and d <= 7 and not ev("play_started"),
                    f"signup {d and round(d, 1)} days ago, plays={len(ev('play_started'))}")
        if gid == "G02":
            return not ev("episode_completed"), f"{len(ev('episode_completed'))} completed episodes on record"
        if gid == "G03":
            d = days_since(signup)
            return d is not None and d <= 2, f"signup {d and round(d, 1)} days ago"
        if gid == "G04":
            return status == "trial_active", f"status from data: {status} (profile rows/events must agree)"
        if gid == "G05":
            return (status == "never" and not paid_events and not from_event,
                    f"status from data: {status}, paid events={len(paid_events)}")
        if gid == "G06":
            lapses = [e for e in ev("subscription_lapsed") if days_since(ts(e["occurred_at_est"])) <= 30]
            return status == "lapsed" and bool(lapses), f"status {status}, lapse events in 30 days={len(lapses)}"
        if gid in ("G08", "G09"):
            if status != "active":
                return False, f"status from data: {status}"
            if gid == "G09":
                days = {ist_day(e["occurred_at_est"]) for e in ev("play_started")
                        if days_since(ts(e["occurred_at_est"])) <= 14}
                return len(days) <= 1, f"play days in 14 days: {len(days)}"
            return True, None
        if gid == "G07":
            slug = ((goal.get("success") or {}).get("target") or {}).get("content_slug")
            plays = [e for e in self.events if e["event"] in ("play_started", "play_progress", "episode_completed")
                     and self.pack.slug_of(e["properties"].get("title_id")) == slug]
            if not plays:
                return False, f"no play of {slug} on record"
            last = max(ts(e["occurred_at_est"]) for e in plays)
            eps = self.pack.episodes.get(slug, {}).get("episode_ids", [])
            furthest = max((eps.index(e["properties"].get("episode_id")) + 1 for e in plays
                            if e["properties"].get("episode_id") in eps), default=0)
            return (days_since(last) >= 3 and 0 < furthest < len(eps),
                    f"{slug}: furthest episode {furthest}/{len(eps)}, idle {round(days_since(last), 1)} days")
        if gid == "G12":
            tid = trigger["event_id"] if trigger else None
            visit = ts(trigger.get("occurred_at_est") or (trigger.get("_q") or {}).get("occurred_at_est")
                       or trigger["received_at"]) if trigger else self.now
            opens = [ts(e["occurred_at_est"]) for e in self.events
                     if e["event"] in ("app_open", "login_success") and e["event_id"] != tid
                     and ts(e["occurred_at_est"]) < visit]
            return (bool(opens) and (visit - max(opens)).days >= 14,
                    f"last earlier open: {max(opens).isoformat() if opens else None}")
        return False, f"no verifier rule for {gid}"

    # ------------------------------------------------------------------ home page
    def verify_fallback_page(self, page, trigger, now):
        """Catalogue-only checks for the degraded-store fallback page (no viewer data can be read). The caller has
        already confirmed the arm; the duplicate-page check needs the store and is left to the caller."""
        passed, failed = [], []
        for name, ok, detail in self._page_static(page, trigger, page.get("published_at") or ts(now)):
            if ok:
                passed.append(name)
            else:
                failed.append({"check": name, "detail": detail, "action": 0})
        return ([page] if not failed else []), {"passed": passed, "failed": failed}

    def _page(self, user_id, a, trigger):
        at = a.get("published_at") or self.now
        out = self._page_static(a, trigger, at)
        rows = a.get("rows") or []
        live = [x for x in self.store.actions_for(user_id) if x["type"] == "home_page" and x.get("published_at")
                and ts(x["published_at"]) <= ts(at) < ts(x["published_at"]) + PAGE_TTL
                and x["dialect"] == a["dialect"] and x.get("hero_title_id") == a.get("hero_title_id")
                and [r["title_ids"] for r in x["rows"]] == [r["title_ids"] for r in rows]]
        out.append(("duplicate_page", not live, f"identical page {live and live[-1]['action_id']} is still live"))
        return out

    def _page_static(self, a, trigger, at):
        out = []
        app_d = (trigger or {}).get("app_dialect")
        out.append(("page_dialect", a["dialect"] == app_d,
                    f"page dialect {a['dialect']} but the app requests {app_d}"))
        rows = a.get("rows") or []
        slugs, bad_rows = [], []
        if len(rows) > 6:
            bad_rows.append(f"{len(rows)} rows (max 6)")
        for r in rows:
            n = len(r["title_ids"])
            if n < 3 or n > 10:
                bad_rows.append(f"row {r['row_type']} has {n} titles")
        ids = ([a["hero_title_id"]] if a.get("hero_title_id") else []) + [t for r in rows for t in r["title_ids"]]
        for tid in ids:
            slugs.append(self.pack.slug_of(tid))
        if len(slugs) != len(set(slugs)):
            bad_rows.append("same content_slug appears twice")
        if not ids:
            bad_rows.append("empty page")
        out.append(("page_rules", not bad_rows, "; ".join(bad_rows)))
        unavailable = [t for t in ids if not self.catalog.available(t, at)]
        wrong = [t for t in ids if (self.pack.titles.get(t) or {}).get("dialect") != a["dialect"]]
        out.append(("availability", not unavailable, f"not available at {at}: {unavailable[:5]}"))
        out.append(("dialect", not wrong, f"not in page dialect {a['dialect']}: {wrong[:5]}"))
        flagged = [t for t in ids if INJECTION.search((self.pack.titles.get(t) or {}).get("description") or "")]
        out.append(("injection", not flagged, f"titles whose description carries instructions: {flagged}"))
        return out

    # ------------------------------------------------------------------ messages
    def _consent(self, channel):
        col = "notifications_opt_in" if channel == "push" else "whatsapp_opt_in"
        if not self.profiles:
            return False, "no profile row: no recorded consent"
        if not all(p.get(col) is True for p in self.profiles):
            return False, f"{col} is not true in every profile row"
        outs = [e for e in self.events if e["event"] == "notification_opt_out"
                and e["properties"].get("channel") == channel]
        if outs:
            return False, f"opt-out event {outs[-1]['event_id']}"
        return True, None

    def _message(self, user_id, a):
        out = []
        ch, send = a["type"], ts(a.get("send_at") or a.get("published_at") or self.now)
        ok, why = self._consent(ch)
        out.append(("consent", ok, why))
        h = send.astimezone(IST).strftime("%H:%M")
        quiet = h >= self.quiet[0] or h < self.quiet[1]
        out.append(("quiet_hours", not quiet, f"send time {h} IST is inside quiet hours"))
        day = ist_day(send)
        ours = [x for x in self.store.actions_for(user_id) if x["type"] in ("push", "whatsapp")
                and ist_day(x.get("send_at") or x["published_at"]) == day]
        delivered = [e for e in self.events if e["event"] == "message_delivered"
                     and e["properties"].get("channel") in ("push", "whatsapp") and ist_day(e["received_at"]) == day]
        out.append(("frequency_cap", not ours and not delivered,
                    f"{len(ours) + len(delivered)} message(s) already on IST day {day}"))
        tid = a.get("title_id")
        if tid:
            t = self.pack.titles.get(tid)
            app_d = self._current_dialect()
            out.append(("message_title", bool(t) and self.catalog.available(tid, send) and t["dialect"] == app_d
                        and not INJECTION.search(t["description"] or ""),
                        f"title {tid} must exist, be available at send time and be in the app dialect {app_d}"))
        if ch == "push":
            out.append(("copy", *self._copy(a)))
        else:
            out.append(("template", *self._template(a)))
        return out

    def _in_app(self, user_id, a):
        """In-app message: no consent and no quiet hours (it is shown inside the app), at most 1 per IST day, and the
        same title and copy rules as a push."""
        send = ts(a.get("send_at") or a.get("published_at") or self.now)
        day = ist_day(send)
        ours = [x for x in self.store.actions_for(user_id) if x["type"] == "in_app_message" and not x.get("cancelled")
                and ist_day(x.get("send_at") or x["published_at"]) == day]
        limit = self.pack.interventions.get("in_app_message", {}).get("max_per_day", 1)
        out = [("in_app_cap", len(ours) < limit, f"{len(ours)} in-app message(s) already on IST day {day}")]
        tid = a.get("title_id")
        t = self.pack.titles.get(tid)
        app_d = self._current_dialect()
        out.append(("message_title", bool(t) and self.catalog.available(tid, send) and t["dialect"] == app_d
                    and not INJECTION.search(t["description"] or ""),
                    f"title {tid} must exist, be available and be in the app dialect {app_d}"))
        out.append(("copy", *self._copy(a)))
        return out

    def _current_language(self):
        ls = [e["app_language"] for e in self.events if e.get("app_language")]
        return ls[-1] if ls else None

    def _current_dialect(self):
        ds = [e["app_dialect"] for e in self.events if e.get("app_dialect")]
        return ds[-1] if ds else None

    def _copy(self, a):
        t, b = (a.get("text") or {}).get("title", ""), (a.get("text") or {}).get("body", "")
        problems = []
        if not t or len(t) > 40:
            problems.append(f"title length {len(t)}")
        if not b or len(b) > 120:
            problems.append(f"body length {len(b)}")
        low = (t + " " + b).lower()
        problems += [f"forbidden phrase '{p}'" for p in self.forbidden if p in low]
        for amount in re.findall(r"(?:₹|rs\.?|inr)\s*(\d+)", low):
            if int(amount) not in self.prices:
                problems.append(f"price ₹{amount} is not in pricing.json")
        if INJECTION.search(low):
            problems.append("copy contains instruction-like text")
        if a.get("deeplink") != f"ott://title/{a.get('title_id')}":
            problems.append("deeplink does not match the title")
        return not problems, "; ".join(problems)

    def _template(self, a):
        tpl = self.templates.get(a.get("template_id"))
        if not tpl:
            return False, f"template {a.get('template_id')} is not approved"
        v = a.get("variables") or {}
        if set(v) != set(tpl["variables"]):
            return False, f"variables {sorted(v)} != template variables {tpl['variables']}"
        if "plan_price" in v:
            m = re.fullmatch(r"₹(\d+)", str(v["plan_price"]))
            if not m or int(m[1]) not in self.prices:
                return False, f"plan_price {v['plan_price']!r} is not a price in pricing.json"
        if "title_name" in v:
            t = self.pack.titles.get(a.get("title_id"))
            if not t or v["title_name"] != t["title"]:
                return False, "title_name must be the catalogue title of title_id"
        if "dialect_name" in v:
            t = self.pack.titles.get(a.get("title_id"))
            if not t or v["dialect_name"] != t["dialect"]:
                return False, "dialect_name must come from the catalogue row of title_id (no free text)"
        if tpl.get("language") and tpl["language"] != self._current_language():
            return False, f"template language {tpl['language']} does not match the app language"
        return True, None
