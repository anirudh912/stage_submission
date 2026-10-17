"""Fact resolver: turns raw events + profile versions into facts with provenance.

Every fact says where it came from (source + refs), how old it is, how confident we are, which other sources
disagreed and which precedence rule decided. Nothing is invented: if no source supports a value it is None.

Precedence rules (rule ids appear on each fact):
  R-status-1   a status-changing event newer than the profile row beats the profile (purchase/renew/lapse)
  R-status-2   otherwise the newest profile row, downgraded to LOW confidence if profile rows disagree
  R-paid-1     "ever paid" = any paid purchase/renewal event, or any profile row saying active/lapsed
  R-dialect-1  the trigger's app setting decides the page dialect (the app only requests that dialect);
               profile, latest dialect change and deeplink are kept as visible conflicts
  R-consent-1  profile opt-in, overridden by any later notification_opt_out event; no profile => no consent
  R-signup-1   signup_completed event beats the profile signup_at; conflicts recorded
  R-time-1     event time = phone occurred_at corrected by measured clock skew (received_at - sent_at)
  R-asof-1     only events with received_at <= decision time are used (no peeking at the future)
"""
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from .pack import CATALOGUE_DIALECTS
from .timeutil import age_s, ist_day, iso, ts

PROFILE_STALE_H = 36       # profile older than this is "stale": fine for slow facts, weak for status
RECENT_LAPSE_DAYS = 30     # G06 "recently lapsed" (definition we chose; goals.json leaves it open)
SERIES_IDLE_DAYS = 3       # G07 "not played it for 3 or more days" (goals.json; not ours to change)
SERIES_MIN_COMPLETED = 1   # our policy on top of goals.json: a viewer who only sampled a series is not "resuming"
TRIAL_DAYS = 3             # pricing.json P_TRIAL


@dataclass
class Fact:
    name: str
    value: object
    source: str                      # event | profile | app_setting | derived | none
    refs: list = field(default_factory=list)
    as_of: str = None                # when the source observed it (server clock where possible)
    age_s: float = None              # decision time - as_of
    confidence: str = "high"         # high | medium | low
    conflicts: list = field(default_factory=list)
    rule: str = None
    note: str = None

    def to_dict(self):
        return asdict(self)


def eref(e):
    return f"event:{e['event_id']}"


def pref(p):
    return f"profile:{p['user_id']}@{p['updated_at']}"


class ViewerView:
    """What the agent knows about one viewer at one moment."""

    def __init__(self, user_id, now, trigger, facts, events, derived):
        self.user_id, self.now, self.trigger = user_id, now, trigger
        self.facts = facts            # name -> Fact
        self.events = events          # events known at `now`, oldest first
        self.derived = derived        # bulky helper data (play history by slug, ...)

    def f(self, name):
        return self.facts[name]

    def value(self, name):
        return self.facts[name].value

    def fact_list(self):
        return list(self.facts.values())

    def conflicts(self):
        return [{"fact": f.name, "used": f.value, "source": f.source, "rule": f.rule, "conflicts": f.conflicts}
                for f in self.facts.values() if f.conflicts]


def _when(e):
    return ts(e.get("occurred_at_est") or e["received_at"])


def resolve(store, pack, user_id, now, trigger=None):
    now = ts(now)
    if trigger is not None:
        trigger = dict(trigger)
        trigger.setdefault("occurred_at_est", (trigger.get("_q") or {}).get("occurred_at_est")
                           or trigger["received_at"])
    events = store.events_for(user_id, until=now)                       # R-asof-1
    trig_id = trigger["event_id"] if trigger else None
    profiles = store.profile_versions(user_id)
    prof = profiles[0] if profiles else None
    facts = {}

    def put(f):
        if f.as_of is not None and f.age_s is None:
            f.age_s = age_s(f.as_of, now)
        facts[f.name] = f

    by_type = {}
    for e in events:
        by_type.setdefault(e["event"], []).append(e)

    # ---------------------------------------------------------------- profile presence / freshness
    if prof:
        stale = age_s(prof["updated_at"], now) > PROFILE_STALE_H * 3600
        put(Fact("profile", {"versions": len(profiles), "stale": stale}, "profile", [pref(p) for p in profiles],
                 prof["updated_at"], confidence="medium" if stale else "high",
                 note="snapshot is a daily export; newer events override status fields"))
    else:
        put(Fact("profile", None, "none", confidence="low",
                 note="no warehouse row (likely signed up after the snapshot)"))

    # ---------------------------------------------------------------- signup
    signup_ev = (by_type.get("signup_completed") or [None])[0]
    conflicts = []
    if signup_ev:
        val, f = iso(_when(signup_ev)), Fact("signup_at", None, "event", [eref(signup_ev)], signup_ev["received_at"],
                                            rule="R-signup-1")
        if prof and prof.get("signup_at") and abs((ts(prof["signup_at"]) - _when(signup_ev)).total_seconds()) > 3600:
            conflicts.append({"source": "profile", "value": prof["signup_at"], "ref": pref(prof)})
        f.value, f.conflicts = val, conflicts
        put(f)
    elif prof and prof.get("signup_at"):
        put(Fact("signup_at", prof["signup_at"], "profile", [pref(prof)], prof["updated_at"], rule="R-signup-1"))
    else:
        put(Fact("signup_at", None, "none", confidence="low", rule="R-signup-1",
                 note="no signup event and no profile row"))

    # ---------------------------------------------------------------- subscription status
    status_events = [e for e in events if e["event"] in
                     ("purchase_completed", "subscription_renewed", "subscription_lapsed")]
    prof_status = {p["subscription_status"] for p in profiles}
    newer = [e for e in status_events if not prof or e["received_at"] > prof["updated_at"]]
    if newer:
        e = newer[-1]
        p = e["properties"]
        st = ("lapsed" if e["event"] == "subscription_lapsed"
              else "trial_active" if p.get("is_trial") else "active")
        f = Fact("subscription_status", st, "event", [eref(e)], e["received_at"], rule="R-status-1",
                 note=f"{e['event']} after the profile snapshot")
        if prof and prof["subscription_status"] != st:
            f.conflicts.append({"source": "profile", "value": prof["subscription_status"], "ref": pref(prof)})
        put(f)
    elif prof:
        f = Fact("subscription_status", prof["subscription_status"], "profile", [pref(prof)], prof["updated_at"],
                 rule="R-status-2", confidence="medium" if facts["profile"].value["stale"] else "high")
        if len(prof_status) > 1:
            f.confidence = "low"
            f.conflicts = [{"source": "profile_duplicate", "value": p["subscription_status"], "ref": pref(p)}
                           for p in profiles[1:] if p["subscription_status"] != prof["subscription_status"]]
            f.note = "duplicate warehouse rows disagree; no later event settles it"
        if prof["subscription_status"] == "active" and prof.get("plan_ends_at") and ts(prof["plan_ends_at"]) < now:
            f.confidence = "low"
            f.conflicts.append({"source": "profile.plan_ends_at", "value": prof["plan_ends_at"],
                                "ref": pref(prof), "why": "plan end passed with no renewal event"})
        put(f)
    else:
        put(Fact("subscription_status", None, "none", confidence="low", rule="R-status-2"))

    # ---------------------------------------------------------------- ever paid / trial end
    paid_ev = [e for e in status_events if e["event"] == "subscription_renewed"
               or (e["event"] == "purchase_completed" and not e["properties"].get("is_trial"))]
    if paid_ev:
        put(Fact("ever_paid", True, "event", [eref(paid_ev[0])], paid_ev[0]["received_at"], rule="R-paid-1"))
    elif profiles:
        paid_rows = [p for p in profiles if p["subscription_status"] in ("active", "lapsed")]
        f = Fact("ever_paid", bool(paid_rows), "profile", [pref(p) for p in (paid_rows or profiles)],
                 prof["updated_at"], rule="R-paid-1", confidence="medium")
        if paid_rows and len(prof_status) > 1:
            f.confidence = "low"
            f.conflicts = [{"source": "profile_duplicate", "value": p["subscription_status"], "ref": pref(p)}
                           for p in profiles if p not in paid_rows]
        put(f)
    else:
        put(Fact("ever_paid", None, "none", confidence="low", rule="R-paid-1"))

    trial_ev = [e for e in status_events if e["event"] == "purchase_completed" and e["properties"].get("is_trial")]
    if facts["subscription_status"].value == "trial_active":
        if trial_ev:
            put(Fact("trial_ends_at", iso(_when(trial_ev[-1]) + timedelta(days=TRIAL_DAYS)), "derived",
                     [eref(trial_ev[-1]), "pricing:P_TRIAL"], trial_ev[-1]["received_at"]))
        elif prof and prof.get("plan_ends_at"):
            put(Fact("trial_ends_at", prof["plan_ends_at"], "profile", [pref(prof)], prof["updated_at"]))
        else:
            put(Fact("trial_ends_at", None, "none", confidence="low"))

    # ---------------------------------------------------------------- dialect / language (R-dialect-1)
    src_evt = trigger or (events[-1] if events else None)
    app_d = src_evt.get("app_dialect") if src_evt else None
    others = []
    if prof and prof.get("preferred_dialect") and prof["preferred_dialect"] != app_d:
        others.append({"source": "profile", "value": prof["preferred_dialect"], "ref": pref(prof)})
    changes = by_type.get("app_dialect_changed") or []
    if changes and changes[-1]["properties"].get("to") != app_d:
        others.append({"source": "app_dialect_changed", "value": changes[-1]["properties"].get("to"),
                       "ref": eref(changes[-1])})
    dl = _deeplink_dialect(trigger) if trigger else None
    if dl and dl["dialect"] != app_d:
        others.append({"source": "campaign_deeplink", "value": dl["dialect"], "ref": eref(trigger),
                       "campaign": dl["campaign"]})
    if app_d in CATALOGUE_DIALECTS:
        put(Fact("dialect", app_d, "app_setting", [eref(src_evt)], src_evt["received_at"], conflicts=others,
                 rule="R-dialect-1", note="the app requests the home page of its current dialect"))
    else:
        put(Fact("dialect", None, "app_setting", [eref(src_evt)] if src_evt else [],
                 src_evt["received_at"] if src_evt else None, confidence="low", conflicts=others, rule="R-dialect-1",
                 note=f"app dialect {app_d!r} has no catalogue content; other sources are not what the app shows"))
    lang = src_evt.get("app_language") if src_evt else None
    put(Fact("language", lang, "app_setting" if lang else "none", [eref(src_evt)] if src_evt else [],
             src_evt["received_at"] if src_evt else None, confidence="high" if lang else "low"))

    # ---------------------------------------------------------------- consent (R-consent-1)
    for ch, col in (("push", "notifications_opt_in"), ("whatsapp", "whatsapp_opt_in")):
        outs = [e for e in by_type.get("notification_opt_out", []) if e["properties"].get("channel") == ch]
        if outs:
            f = Fact(f"consent_{ch}", False, "event", [eref(outs[-1])], outs[-1]["received_at"], rule="R-consent-1",
                     note="opt-out event overrides the snapshot")
            if prof and prof.get(col):
                f.conflicts.append({"source": "profile", "value": True, "ref": pref(prof)})
            put(f)
        elif prof:
            vals = {p.get(col) for p in profiles}
            f = Fact(f"consent_{ch}", prof.get(col) is True, "profile", [pref(prof)], prof["updated_at"],
                     rule="R-consent-1")
            if len(vals) > 1:   # disagreeing rows: consent must be unanimous
                f.value, f.confidence = False, "low"
                f.conflicts = [{"source": "profile_duplicate", "value": p.get(col), "ref": pref(p)} for p in profiles]
            put(f)
        else:
            put(Fact(f"consent_{ch}", False, "none", confidence="low", rule="R-consent-1",
                     note="no profile row, so no recorded consent"))

    # ---------------------------------------------------------------- viewing history
    plays = by_type.get("play_started", [])
    put(Fact("has_played", bool(plays), "event" if plays else "derived",
             [eref(plays[0])] if plays else [f"absence:play_started:{len(events)}_events_scanned"],
             plays[0]["received_at"] if plays else iso(now)))
    comps = by_type.get("episode_completed", [])
    put(Fact("has_completed_episode", bool(comps), "event" if comps else "derived",
             [eref(comps[0])] if comps else [f"absence:episode_completed:{len(events)}_events_scanned"],
             comps[0]["received_at"] if comps else iso(now)))

    since14 = now - timedelta(days=14)
    days = {}
    for e in plays:
        if _when(e) >= since14:
            days.setdefault(ist_day(_when(e)), e)
    put(Fact("play_days_14d", len(days), "derived", [eref(e) for e in days.values()], iso(now)))

    opens = [e for e in events if e["event"] in ("app_open", "login_success") and e["event_id"] != trig_id]
    ref_time = _when(trigger) if trigger else now
    prior = [e for e in opens if _when(e) < ref_time]
    last_open = max(prior, key=_when) if prior else None
    put(Fact("last_open_before_trigger", iso(_when(last_open)) if last_open else None,
             "event" if last_open else "derived", [eref(last_open)] if last_open else
             [f"absence:app_open:{len(events)}_events_scanned"], last_open["received_at"] if last_open else iso(now),
             note="excludes the trigger itself; uses clock-corrected time"))

    lapses = by_type.get("subscription_lapsed", [])
    recent = [e for e in lapses if _when(e) >= now - timedelta(days=RECENT_LAPSE_DAYS)]
    put(Fact("recent_lapse", iso(_when(recent[-1])) if recent else None, "event" if recent else "derived",
             [eref(recent[-1])] if recent else [f"absence:subscription_lapsed_{RECENT_LAPSE_DAYS}d"],
             recent[-1]["received_at"] if recent else iso(now),
             note=f"G06 'recently lapsed' = lapse event within {RECENT_LAPSE_DAYS} days (our definition)"))

    series = _unfinished_series(pack, plays + comps + by_type.get("play_progress", []), now)
    put(Fact("unfinished_series", [s["summary"] for s in series], "derived",
             [r for s in series for r in s["refs"]], iso(now),
             note=f"show/micro_drama with episodes left, no play for {SERIES_IDLE_DAYS}+ days, and at least "
                  f"{SERIES_MIN_COMPLETED} episode completed (sampling one episode is not an unfinished series)"))

    # ---------------------------------------------------------------- trigger freshness
    if trigger:
        q = trigger.get("_q") or trigger.get("quality") or {}
        deliver = trigger.get("deliver_at") or trigger["received_at"]
        happened = trigger.get("occurred_at_est") or q.get("occurred_at_est") or trigger["received_at"]
        put(Fact("trigger_age_s", age_s(happened, deliver), "derived", [eref(trigger)], deliver,
                 confidence="medium" if q.get("clock_suspect") else "high",
                 note="clock-corrected; phone clock was wrong" if q.get("clock_suspect") else None))

    return ViewerView(user_id, now, trigger, facts, events,
                      {"series": series, "plays": plays, "completions": comps})


def _deeplink_dialect(e):
    dl = ((e or {}).get("properties") or {}).get("deeplink") or ""
    parts = dl.split("/")
    # ott://c/<campaign>/<dialect>/title/<title_id>
    if dl.startswith("ott://c/") and len(parts) == 7 and parts[5] == "title":
        return {"campaign": parts[3], "dialect": parts[4], "title_id": parts[6]}
    return None


def _unfinished_series(pack, watch_events, now):
    by_slug = {}
    for e in watch_events:
        tid = e["properties"].get("title_id")
        t = pack.title(tid)
        if not t or t["content_type"] not in ("show", "micro_drama"):
            continue
        slug = t["content_slug"]
        pos = pack.episode_position(slug, e["properties"].get("episode_id"))
        s = by_slug.setdefault(slug, {"last": None, "max_pos": 0, "title_ids": set(), "last_ref": None,
                                      "completed": set(), "completed_ref": None})
        s["title_ids"].add(tid)
        if e["event"] == "episode_completed":
            s["completed"].add(e["properties"].get("episode_id"))
            s["completed_ref"] = s["completed_ref"] or eref(e)
        if pos:
            s["max_pos"] = max(s["max_pos"], pos)
        if s["last"] is None or _when(e) > s["last"]:
            s["last"], s["last_ref"] = _when(e), eref(e)
    out = []
    for slug, s in by_slug.items():
        total = pack.episodes.get(slug, {}).get("count", 0)
        idle_days = (now - s["last"]).total_seconds() / 86400
        if s["max_pos"] and s["max_pos"] < total and idle_days >= SERIES_IDLE_DAYS \
                and len(s["completed"]) >= SERIES_MIN_COMPLETED:
            next_ep = pack.episodes[slug]["episode_ids"][s["max_pos"]]
            out.append({"slug": slug, "refs": [s["last_ref"], s["completed_ref"], f"catalogue:{slug}"],
                        "summary": {"content_slug": slug, "title_ids": sorted(s["title_ids"]),
                                    "furthest_episode": s["max_pos"], "episodes_total": total,
                                    "episodes_completed": len(s["completed"]),
                                    "next_episode_id": next_ep, "last_played_at": iso(s["last"]),
                                    "idle_days": round(idle_days, 1)}})
    out.sort(key=lambda s: s["summary"]["last_played_at"], reverse=True)
    return out
