"""Model provider abstraction plus the deterministic mock used for replay.

The model is an ADVISOR only. It is asked one narrow question per decision ("which goal fits this viewer?") and its
answer is treated as an untrusted suggestion:
  * it is parsed defensively (malformed output -> ignored),
  * it can only pick among goals the rules already found eligible on cited evidence,
  * its free-text reason is never evidence, never copied into messages, never used for eligibility.
No viewer data leaves the process: the mock runs locally, and a real provider would receive only goal ids and
fact names/values listed in `_request` (no free text from the catalogue).

Every call costs virtual time on the replay clock, so outages and timeouts show up in decision latency.
"""
import json


class ModelError(Exception):
    """Raised for an unavailable provider (HTTP 503) or a timeout."""

    def __init__(self, kind, detail, elapsed_s):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail, self.elapsed_s = kind, detail, elapsed_s


class ModelProvider:
    name = "abstract"

    def advise_goal(self, request, now, timeout_s):
        """Return (raw_output, elapsed_s). raw_output is a str or dict exactly as the provider produced it."""
        raise NotImplementedError


class MockProvider(ModelProvider):
    """Deterministic advisor. Normally agrees with the strongest eligible candidate and says so.
    Reproduces replay/model_faults.jsonl at the named triggers, and the provider outage windows."""

    name = "mock"
    NORMAL_LATENCY_S = 0.4

    def __init__(self, faults=(), outages=None):
        self.faults = {f["trigger_event_id"]: f for f in faults}
        self.outages = outages          # callable(now) -> bool, set by the harness from control events

    def advise_goal(self, request, now, timeout_s):
        if self.outages and self.outages(now):
            raise ModelError("unavailable", "HTTP 503 from model provider", 0.05)
        f = self.faults.get(request.get("trigger_event_id"))
        if f and f["fault"] == "timeout":
            waited = min(float(f.get("seconds", timeout_s)), timeout_s)
            raise ModelError("timeout", f"no answer after {waited:.0f}s (provider would take {f.get('seconds')}s)",
                             waited)
        if f:
            return f["model_output"], self.NORMAL_LATENCY_S
        if request.get("open_goal_id"):
            return {"goal_id": request["open_goal_id"], "eligible": True, "confidence": 0.6,
                    "reason": "keep the goal already in progress"}, self.NORMAL_LATENCY_S
        eligible = [c for c in request["candidates"] if c["eligible"]]
        if not eligible:
            return {"goal_id": None, "eligible": False, "confidence": 0.5,
                    "reason": "no candidate goal is eligible"}, self.NORMAL_LATENCY_S
        top = eligible[0]
        return {"goal_id": top["goal_id"], "eligible": True, "confidence": 0.6,
                "reason": f"agrees with rule ranking: {top['why']}"}, self.NORMAL_LATENCY_S


def build_request(trigger, view, choice, priority):
    """The only data a provider sees: ids, goal candidates in rule order, and a few fact values (no free text)."""
    order = {g: i for i, g in enumerate(priority)}
    cands = sorted(choice.considered, key=lambda c: order.get(c["goal_id"], 99))
    return {
        "trigger_event_id": trigger["event_id"] if trigger else None,
        "viewer_id": view.user_id,
        "open_goal_id": choice.goal_id if choice.continuing else None,
        "candidates": [{"goal_id": c["goal_id"], "eligible": c["eligible"] and not c.get("skipped"),
                        "why": c["why"]} for c in cands],
        "facts": {n: view.value(n) for n in ("subscription_status", "has_played", "has_completed_episode",
                                              "play_days_14d", "dialect")},
    }


def parse_advice(raw):
    """Defensive parse. Returns (advice_dict | None, problem | None)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as e:
            return None, f"malformed model output ({e.__class__.__name__}): {raw[:60]!r}"
    if not isinstance(raw, dict):
        return None, f"model output is {type(raw).__name__}, expected an object"
    gid = raw.get("goal_id")
    if gid is not None and not isinstance(gid, str):
        return None, "goal_id is not a string"
    conf = raw.get("confidence")
    if conf is not None and not isinstance(conf, (int, float)):
        return None, "confidence is not a number"
    return {"goal_id": gid, "eligible": bool(raw.get("eligible")), "confidence": conf,
            "reason": str(raw.get("reason", ""))[:200]}, None


class CircuitBreaker:
    """After `threshold` consecutive provider failures, skip calls for `cooldown_s` of replay time (fail fast)."""

    def __init__(self, threshold=3, cooldown_s=60):
        self.threshold, self.cooldown_s = threshold, cooldown_s
        self.failures, self.open_until = 0, None

    def allow(self, now_s):
        return self.open_until is None or now_s >= self.open_until

    def record(self, ok, now_s):
        if ok:
            self.failures, self.open_until = 0, None
        else:
            self.failures += 1
            if self.failures >= self.threshold:
                self.open_until = now_s + self.cooldown_s
