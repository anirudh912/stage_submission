"""Dependency wrapper for the user data store: counts tool calls, caches reads within one decision, and injects
the replay's degradation (slow reads, timeouts) on the virtual clock. Writes go to the agent's own journal and are
not degraded.

Read policy during one decision:
  * each distinct read is one tool call; a repeat read in the same decision is served from the decision cache
  * a read that times out is retried once (another tool call)
  * READ_TIMEOUT_S per attempt; if the decision has spent DECISION_IO_BUDGET_S waiting, stop reading
"""
import hashlib

from .timeutil import iso, ts

READ_TIMEOUT_S = 10.0
DECISION_IO_BUDGET_S = 40.0

READ_METHODS = ("events_for", "profile_versions", "open_goal", "actions_for", "arm", "decision_for_trigger",
                "goals_for", "open_goals_of_type", "goal", "learnings_for")


class DataStoreTimeout(Exception):
    pass


def _u(*parts):
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:12], 16) / 16 ** 12


class StoreProxy:
    def __init__(self, store, degraded=None, max_tool_calls=20):
        self._store = store
        self.degraded = degraded or (lambda now: None)     # callable(now) -> None | {"p95_s":.., "timeout_rate":..}
        self.max_tool_calls = max_tool_calls
        self.begin("init", None)

    def begin(self, key, now):
        self.key, self.now = key, now
        self.cache, self.tool_calls, self.wait_s, self.retries, self.timeouts = {}, 0, 0.0, 0, 0

    def __getattr__(self, name):
        attr = getattr(self._store, name)
        if name not in READ_METHODS:
            return attr                                        # writes and helpers pass straight through

        def read(*args, **kw):
            k = (name, args, tuple(sorted(kw.items())))
            if k in self.cache:
                return self.cache[k]
            for attempt in (1, 2):
                if self.tool_calls >= self.max_tool_calls:
                    raise DataStoreTimeout(f"tool-call budget ({self.max_tool_calls}) used up before {name}")
                if self.wait_s >= DECISION_IO_BUDGET_S:
                    raise DataStoreTimeout(f"spent {self.wait_s:.0f}s waiting on the data store; stop reading")
                self.tool_calls += 1
                ok, latency = self._attempt(name, attempt)
                self.wait_s += latency
                if ok:
                    break
                self.timeouts += 1
                if attempt == 1:
                    self.retries += 1
            else:
                raise DataStoreTimeout(f"{name} timed out twice (data store degraded)")
            out = attr(*args, **kw)
            self.cache[k] = out
            return out
        return read

    def _attempt(self, name, attempt):
        d = self.degraded(self.now) if self.now else None
        if not d:
            return True, 0.0
        u = _u(self.key, name, attempt, self.tool_calls)
        if u < d["timeout_rate"]:
            return False, READ_TIMEOUT_S
        # latency spread so that about 95% of answered reads are below p95_s
        lat = 0.2 + (d["p95_s"] - 0.2) * ((u - d["timeout_rate"]) / (1 - d["timeout_rate"])) ** 4 / 0.95 ** 4
        if lat > READ_TIMEOUT_S:
            return False, READ_TIMEOUT_S
        return True, round(lat, 2)


class Windows:
    """Time windows from control events, e.g. a provider outage. Harness-side fault injection."""

    def __init__(self):
        self.items = []

    def add(self, start, end, info=True):
        self.items.append((iso(ts(start)), iso(ts(end)), info))

    def __call__(self, now):
        n = iso(ts(now))
        for s, e, info in self.items:
            if s <= n < e:
                return info
        return None
