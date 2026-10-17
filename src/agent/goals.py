"""Goal rules: eligibility, choice, revision, success detection and rule corrections.

Eligibility is plain code over resolved facts. Each check returns the evidence refs it relied on, and a goal whose
deciding fact has LOW confidence is not chosen (the agent says why instead).

Goals we never choose:
  G10 meaningful_engagement_7d: success event is null, so it can never be judged
  G11 tv_activation_14d: tv_app_open does not exist in the event stream
Underspecified goals, and the definitions we picked:
  G06 "recently lapsed" = subscription_lapsed event within 30 days (resolver.RECENT_LAPSE_DAYS)
  G07 "unwatched episodes left" = furthest episode reached < episodes in the content_slug, idle 3+ days;
      we also require at least one COMPLETED episode of that series (policy: narrows goals.json, never widens it)
"""
import copy
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from .timeutil import ist_day, iso, ts

UNMEASURABLE = {
    "G10": "success event is null ('engages meaningfully' cannot be measured)",
    "G11": "success event tv_app_open never appears in the event stream",
}
NEW_RELEASE_DAYS = 30

# Lifecycle order: time-sensitive money first, then first experiences, then habits, then money for engaged users.
PRIORITY = ["G04", "G01", "G02", "G03", "G07", "G06", "G12", "G05", "G09", "G08"]


@dataclass
class Eligibility:
    goal_id: str
    eligible: bool
    reasons: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    blocked_by_low_confidence: bool = False

    def to_dict(self):
        return asdict(self)


class GoalRegistry:
    """Goal definitions plus versioned success rules. A correction bumps the version of one goal only."""

    def __init__(self, goals_config):
        self.goals = {g["goal_id"]: copy.deepcopy(g) for g in goals_config["goals"]}
        self.versions = {gid: 1 for gid in self.goals}
        self.corrections = []

    def spec(self, goal_id):
        return self.goals[goal_id]

    def success_spec(self, goal_id):
        g = self.goals[goal_id]
        return {**g["success"], "goal_id": goal_id, "rule_version": self.versions[goal_id]}

    def rule_ref(self, goal_id, part):
        return f"goal_rule:{goal_id}-{part}-v{self.versions[goal_id]}"

    def apply_correction(self, ctrl):
        """Apply a goal_rule_corrected control event. Returns the new success spec."""
        gid = ctrl["goal_id"]
        old = self.goals[gid]["success"]
        if ctrl.get("old_success") and ctrl["old_success"].get("event") != old.get("event"):
            raise ValueError(f"correction for {gid} expects old event {ctrl['old_success']['event']!r}, "
                             f"registry has {old.get('event')!r}")
        self.goals[gid]["success"] = {**old, **ctrl["new_success"]}
        self.versions[gid] += 1
        self.corrections.append({"at": ctrl["at"], "goal_id": gid, "version": self.versions[gid],
                                 "note": ctrl.get("note")})
        return self.success_spec(gid)


def apply_rule_correction(store, registry, ctrl):
    """Correct one goal's success rule and re-point ONLY the open goals of that type (not every viewer).
    Returns the affected goal_instance_ids so the caller can re-judge just those."""
    spec = registry.apply_correction(ctrl)
    affected = []
    for g in store.open_goals_of_type(ctrl["goal_id"]):
        new = {**spec, **({"target": g["success_spec"]["target"]} if "target" in g["success_spec"] else {})}
        store.update_goal_rule(g["goal_instance_id"], new, spec["rule_version"], ctrl["at"],
                               ctrl.get("note") or "goal_rule_corrected")
        affected.append(g["goal_instance_id"])
    return affected


# ------------------------------------------------------------------- eligibility

def _signed_up_within(view, days):
    f = view.f("signup_at")
    if f.value is None:
        return None, f
    return (view.now - ts(f.value)) <= timedelta(days=days), f


def eligibility(view, registry, pack):
    """Evaluate every goal. Returns {goal_id: Eligibility}."""
    out = {}
    R = registry.rule_ref

    def res(gid, ok, why, ev=(), low=False):
        out[gid] = Eligibility(gid, bool(ok) and not low, [why], list(ev), low)

    for gid, why in UNMEASURABLE.items():
        res(gid, False, why, [R(gid, "success")])

    # G01 / G03: signup recency
    for gid, days in (("G01", 7), ("G03", 2)):
        within, f = _signed_up_within(view, days)
        if within is None:
            res(gid, False, "signup time unknown", [R(gid, "elig")])
            continue
        ev = f.refs + [R(gid, "elig")]
        if not within:
            res(gid, False, f"signed up more than {days} days ago ({f.value})", ev)
        elif gid == "G01" and view.value("has_played"):
            res(gid, False, "already started a play", ev + view.f("has_played").refs)
        else:
            extra = view.f("has_played").refs if gid == "G01" else []
            res(gid, True, f"signed up {f.value}, within {days} days" +
                (", no play_started yet" if gid == "G01" else ""), ev + extra, low=f.confidence == "low")

    # G02: never completed an episode
    f = view.f("has_completed_episode")
    res("G02", not f.value, "has completed an episode" if f.value else "no episode_completed in history",
        f.refs + [R("G02", "elig")])

    # status-based goals
    st = view.f("subscription_status")
    low_st = st.confidence == "low"
    st_ev = st.refs + [c["ref"] for c in st.conflicts if c.get("ref")]
    res("G04", st.value == "trial_active", f"subscription_status={st.value} ({st.source})",
        st_ev + [R("G04", "elig")], low=low_st)

    paid = view.f("ever_paid")
    ok = st.value == "never" and paid.value is False
    res("G05", ok, f"subscription_status={st.value}, ever_paid={paid.value}",
        st_ev + paid.refs + [R("G05", "elig")], low=low_st or paid.confidence == "low")

    lapse = view.f("recent_lapse")
    if st.value == "lapsed" and lapse.value:
        res("G06", True, f"lapsed, lapse event at {lapse.value}", st_ev + lapse.refs + [R("G06", "elig")], low=low_st)
    else:
        res("G06", False, f"subscription_status={st.value}; recent lapse event: {lapse.value or 'none'}",
            st_ev + lapse.refs + [R("G06", "elig")])

    for gid in ("G08", "G09"):
        if st.value != "active":
            res(gid, False, f"not an active subscriber (status={st.value})", st_ev + [R(gid, "elig")])
    if st.value == "active":
        d = view.value("dialect")
        recent = [t for t in pack.titles.values() if t["dialect"] == d and _available(t, view.now)
                  and _released_within(t, view.now, NEW_RELEASE_DAYS)]
        res("G08", bool(recent),
            f"{len(recent)} available releases from the last {NEW_RELEASE_DAYS} days in dialect {d}" if recent
            else f"no release from the last {NEW_RELEASE_DAYS} days available in dialect {d}: goal not achievable",
            st_ev + [R("G08", "elig")] + [f"catalogue:{t['title_id']}" for t in recent[:3]], low=low_st)
        pd = view.f("play_days_14d")
        res("G09", pd.value <= 1, f"plays on {pd.value} IST day(s) in the last 14 days",
            st_ev + pd.refs + [R("G09", "elig")], low=low_st)

    # G07: unfinished series
    s = view.f("unfinished_series")
    res("G07", bool(s.value), f"{len(s.value)} unfinished series idle 3+ days" if s.value
        else "no unfinished series idle 3+ days", s.refs + [R("G07", "elig")])

    # G12: return after 14 days without an app open
    lo = view.f("last_open_before_trigger")
    if lo.value is None:
        # never opened before: that is a new viewer, not one who "stopped visiting"
        res("G12", False, "no earlier app_open on record: new viewer, not a returning one", lo.refs + [R("G12", "elig")])
    else:
        visit = ts(view.trigger["occurred_at_est"]) if view.trigger else view.now
        gap = (visit - ts(lo.value)).days
        res("G12", gap >= 14, f"last app_open {gap} days before this visit", lo.refs + [R("G12", "elig")])
    return out


def _available(t, at):
    at = iso(ts(at))
    return (t["available_from"] is None or t["available_from"] <= at) and \
        (t["available_until"] is None or at < t["available_until"])


def _released_within(t, at, days):
    rd = t.get("release_date")
    return bool(rd) and (ts(at).date() - timedelta(days=days)).isoformat() <= rd <= ts(at).date().isoformat()


# ------------------------------------------------------------------- choice

@dataclass
class GoalChoice:
    goal_id: str = None
    deadline: str = None
    success: dict = None
    evidence: list = field(default_factory=list)
    reason: str = None
    confidence: float = 0.0
    confidence_reason: str = None
    continuing: str = None           # goal_instance_id when an open goal is kept
    considered: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


_CONF = {"high": 0.9, "medium": 0.7, "low": 0.3}


def choose_goal(view, registry, pack, open_goal=None, exclude=()):
    """Pick ONE goal. Keeps the open goal if it still applies (single active plan); otherwise the first eligible
    goal in PRIORITY order. `exclude` lets the action step ask for the next goal when one is not actionable."""
    elig = eligibility(view, registry, pack)
    considered = [{"goal_id": gid, "eligible": e.eligible, "why": e.reasons[0],
                   **({"blocked_by_low_confidence": True} if e.blocked_by_low_confidence else {})}
                  for gid, e in sorted(elig.items())]

    if open_goal:
        e = elig.get(open_goal["goal_id"])
        if ts(open_goal["deadline"]) > view.now:
            return GoalChoice(open_goal["goal_id"], open_goal["deadline"], open_goal["success_spec"],
                              (open_goal.get("evidence") or []) + (e.evidence if e else []),
                              f"continuing open goal {open_goal['goal_instance_id']} (one active plan per viewer)",
                              _CONF["high"], "goal already committed; window still open",
                              open_goal["goal_instance_id"], considered)

    for gid in PRIORITY:
        e = elig[gid]
        if gid in exclude or not e.eligible:
            continue
        spec = registry.success_spec(gid)
        if gid == "G07":   # the success rule names the series, so the goal carries it
            series = view.value("unfinished_series")
            here = [x for x in series if x["content_slug"].endswith("-" + str(view.value("dialect")))]
            spec["target"] = (here or series)[0]
        conf, why = _goal_confidence(view, gid)
        return GoalChoice(gid, iso(view.now + timedelta(hours=spec["window_hours"])), spec, e.evidence,
                          e.reasons[0], conf, why, None, considered)

    blocked = [c["goal_id"] for c in considered if c.get("blocked_by_low_confidence")]
    reason = "no eligible goal"
    if blocked:
        reason += f"; {', '.join(blocked)} would apply but rest on conflicting or stale evidence"
    return GoalChoice(None, None, None, [], reason, 0.0, "nothing measurable to pursue yet", None, considered)


def _goal_confidence(view, gid):
    deciding = {"G01": ["signup_at", "has_played"], "G02": ["has_completed_episode"], "G03": ["signup_at"],
                "G04": ["subscription_status"], "G05": ["subscription_status", "ever_paid"],
                "G06": ["subscription_status", "recent_lapse"], "G07": ["unfinished_series"],
                "G08": ["subscription_status"], "G09": ["subscription_status", "play_days_14d"],
                "G12": ["last_open_before_trigger"]}[gid]
    fs = [view.f(n) for n in deciding]
    worst = min(fs, key=lambda f: _CONF[f.confidence])
    score = _CONF[worst.confidence]
    if view.facts["profile"].value is None:
        score -= 0.1
    why = f"weakest deciding fact: {worst.name} ({worst.confidence}, source {worst.source}" + \
          (f", {int(worst.age_s // 3600)}h old)" if worst.age_s else ")")
    return round(max(score, 0.05), 2), why


# ------------------------------------------------------------------- success detection (used by the judge too)

def success_event(goal, events, pack):
    """First event that satisfies the goal's CURRENT success spec inside its window, or None.
    `events` must already be the attributable stream (server-received, deduplicated)."""
    spec = goal["success_spec"]
    gid = goal["goal_id"]
    start, end = ts(goal["started_at"]), ts(goal["deadline"])
    if not spec.get("event"):
        return None
    hits = [e for e in events if e["event"] == spec["event"] and start <= _t(e) <= end]
    if gid == "G03":
        start_day = ist_day(start)
        hits = [e for e in hits if ist_day(_t(e)) > start_day]
    elif gid in ("G04", "G05", "G06"):
        hits = [e for e in hits if not e["properties"].get("is_trial")]
    elif gid == "G07":
        slug = (spec.get("target") or {}).get("content_slug")
        hits = [e for e in hits if slug and pack.slug_of(e["properties"].get("title_id")) == slug]
    elif gid == "G08":
        hits = [e for e in hits if _released_within(pack.title(e["properties"].get("title_id")) or {},
                                                    start, NEW_RELEASE_DAYS)]
    elif gid == "G09":
        days, out = set(), None
        for e in sorted(hits, key=_t):
            days.add(ist_day(_t(e)))
            if len(days) >= 3:
                out = e
                break
        return out
    return min(hits, key=_t) if hits else None


def _t(e):
    return ts(e.get("occurred_at_est") or e["received_at"])
