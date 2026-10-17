"""Durable per-viewer state (SQLite).

What lives here, per viewer:
  events          every event the agent has received (idempotent on event_id; redeliveries are counted, not stored twice)
  profile_versions every warehouse row for the viewer, including duplicates (conflicts stay visible)
  facts           what the agent believed at each decision, with source, reference and freshness
  goals           goal instances; at most ONE open goal per viewer (enforced by a unique index)
  decisions       one per trigger event (idempotent on trigger_event_id)
  actions         what was published
  learnings       what the agent concluded after a goal closed
"""
import json
import sqlite3

from .timeutil import iso, ts

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS viewers (
  user_id TEXT PRIMARY KEY, arm TEXT, first_seen_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, event TEXT NOT NULL,
  received_at TEXT NOT NULL, occurred_at TEXT, occurred_at_est TEXT, sent_at TEXT,
  app_dialect TEXT, app_language TEXT, session_id TEXT, properties TEXT,
  quality TEXT, source TEXT, copies INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS events_user ON events(user_id, received_at);
CREATE TABLE IF NOT EXISTS profile_versions (
  user_id TEXT NOT NULL, updated_at TEXT NOT NULL, snapshot_at TEXT, row TEXT NOT NULL,
  PRIMARY KEY (user_id, updated_at)
);
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, decision_id TEXT, name TEXT NOT NULL,
  value TEXT, source TEXT, refs TEXT, as_of TEXT, age_s REAL, confidence TEXT, conflicts TEXT,
  rule TEXT, note TEXT, recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_user ON facts(user_id, recorded_at);
CREATE TABLE IF NOT EXISTS goals (
  goal_instance_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, goal_id TEXT NOT NULL,
  status TEXT NOT NULL, started_at TEXT NOT NULL, deadline TEXT NOT NULL,
  success_spec TEXT NOT NULL, rule_version INTEGER NOT NULL, decision_id TEXT,
  evidence TEXT, closed_at TEXT, judgement TEXT, history TEXT NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_goal ON goals(user_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS goals_goal_status ON goals(goal_id, status);
CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, trigger_event_id TEXT UNIQUE,
  status TEXT, created_at TEXT NOT NULL, record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_user ON decisions(user_id, created_at);
CREATE TABLE IF NOT EXISTS actions (
  action_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, user_id TEXT NOT NULL, type TEXT NOT NULL,
  published_at TEXT, send_at TEXT, record TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS actions_user ON actions(user_id, published_at);
CREATE TABLE IF NOT EXISTS learnings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, goal_instance_id TEXT,
  at TEXT NOT NULL, note TEXT NOT NULL
);
"""

EVENT_COLS = ("event_id", "user_id", "event", "received_at", "occurred_at", "occurred_at_est", "sent_at",
              "app_dialect", "app_language", "session_id", "properties", "quality", "source", "copies")


def _j(v):
    return None if v is None else json.dumps(v, separators=(",", ":"), sort_keys=True)


def _uj(v):
    return None if v is None else json.loads(v)


class Store:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def __del__(self):
        try:
            self.db.close()
        except Exception:
            pass

    # ------------------------------------------------------------ viewers / arms
    def set_arms(self, arms):
        self.db.executemany(
            "INSERT INTO viewers(user_id, arm) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET arm=excluded.arm",
            list(arms.items()))
        self.db.commit()

    def arm(self, user_id):
        r = self.db.execute("SELECT arm FROM viewers WHERE user_id=?", (user_id,)).fetchone()
        return r["arm"] if r else None

    # ------------------------------------------------------------ events
    @staticmethod
    def _event_row(e, source):
        q = e.get("_q") or {}
        return (e["event_id"], e["user_id"], e["event"], e["received_at"], e.get("occurred_at"),
                q.get("occurred_at_est") or e.get("occurred_at"), e.get("sent_at"), e.get("app_dialect"),
                e.get("app_language"), e.get("session_id"), _j(e.get("properties") or {}), _j(q), source, 1)

    def ingest_event(self, e, source):
        """Store one event. Returns True if new, False if it was a redelivery (same event_id)."""
        cur = self.db.execute(
            f"INSERT OR IGNORE INTO events({','.join(EVENT_COLS)}) VALUES ({','.join('?' * len(EVENT_COLS))})",
            self._event_row(e, source))
        if cur.rowcount == 0:
            self.db.execute("UPDATE events SET copies = copies + 1 WHERE event_id=?", (e["event_id"],))
        self.db.commit()
        return cur.rowcount == 1

    def bulk_ingest(self, events, source):
        rows = [self._event_row(e, source) for e in events]
        self.db.executemany(
            f"INSERT OR IGNORE INTO events({','.join(EVENT_COLS)}) VALUES ({','.join('?' * len(EVENT_COLS))})", rows)
        self.db.commit()

    def events_for(self, user_id, until=None):
        """A viewer's events known at time `until` (by server received_at), oldest first."""
        sql, args = "SELECT * FROM events WHERE user_id=?", [user_id]
        if until is not None:
            sql += " AND received_at <= ?"
            args.append(iso(ts(until)))
        rows = self.db.execute(sql + " ORDER BY received_at", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["properties"] = _uj(d["properties"]) or {}
            d["quality"] = _uj(d["quality"]) or {}
            out.append(d)
        return out

    def event_copies(self, event_id):
        r = self.db.execute("SELECT copies FROM events WHERE event_id=?", (event_id,)).fetchone()
        return r["copies"] if r else 0

    # ------------------------------------------------------------ profiles
    def load_profiles(self, profiles):
        """profiles: output of prep (user_id -> chosen row + other_versions). Every version is kept."""
        rows = []
        for uid, p in profiles.items():
            for v in [p] + p.get("other_versions", []):
                v = {k: val for k, val in v.items()
                     if k not in ("other_versions", "conflict_fields", "versions", "dialect_valid")}
                rows.append((uid, v["updated_at"], v.get("snapshot_at"), _j(v)))
        self.db.executemany("INSERT OR IGNORE INTO profile_versions VALUES (?,?,?,?)", rows)
        self.db.commit()

    def profile_versions(self, user_id):
        """All warehouse rows for a viewer, newest updated_at first."""
        rows = self.db.execute(
            "SELECT row FROM profile_versions WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()
        return [_uj(r["row"]) for r in rows]

    # ------------------------------------------------------------ facts
    def record_facts(self, user_id, decision_id, facts, recorded_at):
        self.db.executemany(
            "INSERT INTO facts(user_id, decision_id, name, value, source, refs, as_of, age_s, confidence,"
            " conflicts, rule, note, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(user_id, decision_id, f.name, _j(f.value), f.source, _j(f.refs), f.as_of, f.age_s, f.confidence,
              _j(f.conflicts), f.rule, f.note, iso(ts(recorded_at))) for f in facts])
        self.db.commit()

    def facts_at(self, user_id, decision_id):
        rows = self.db.execute("SELECT * FROM facts WHERE user_id=? AND decision_id=? ORDER BY id",
                               (user_id, decision_id)).fetchall()
        return [{**dict(r), "value": _uj(r["value"]), "refs": _uj(r["refs"]), "conflicts": _uj(r["conflicts"])}
                for r in rows]

    # ------------------------------------------------------------ goals
    @staticmethod
    def _goal(r):
        if r is None:
            return None
        d = dict(r)
        for k in ("success_spec", "evidence", "judgement", "history"):
            d[k] = _uj(d[k])
        return d

    def open_goal(self, user_id):
        return self._goal(self.db.execute(
            "SELECT * FROM goals WHERE user_id=? AND status='open'", (user_id,)).fetchone())

    def goal(self, goal_instance_id):
        return self._goal(self.db.execute(
            "SELECT * FROM goals WHERE goal_instance_id=?", (goal_instance_id,)).fetchone())

    def goals_for(self, user_id):
        return [self._goal(r) for r in self.db.execute(
            "SELECT * FROM goals WHERE user_id=? ORDER BY started_at", (user_id,)).fetchall()]

    def open_goals_of_type(self, goal_id):
        return [self._goal(r) for r in self.db.execute(
            "SELECT * FROM goals WHERE goal_id=? AND status='open'", (goal_id,)).fetchall()]

    def create_goal(self, g):
        """Raises sqlite3.IntegrityError if the viewer already has an open goal (single active plan)."""
        self.db.execute(
            "INSERT INTO goals(goal_instance_id, user_id, goal_id, status, started_at, deadline, success_spec,"
            " rule_version, decision_id, evidence, history) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (g["goal_instance_id"], g["user_id"], g["goal_id"], "open", g["started_at"], g["deadline"],
             _j(g["success_spec"]), g["rule_version"], g.get("decision_id"), _j(g.get("evidence", [])),
             _j([{"at": g["started_at"], "change": "opened", "rule_version": g["rule_version"]}])))
        self.db.commit()

    def _append_history(self, goal_instance_id, entry):
        g = self.goal(goal_instance_id)
        self.db.execute("UPDATE goals SET history=? WHERE goal_instance_id=?",
                        (_j(g["history"] + [entry]), goal_instance_id))

    def update_goal_rule(self, goal_instance_id, success_spec, rule_version, at, reason):
        self._append_history(goal_instance_id, {"at": at, "change": "success_rule_corrected",
                                                "rule_version": rule_version, "reason": reason})
        self.db.execute("UPDATE goals SET success_spec=?, rule_version=? WHERE goal_instance_id=?",
                        (_j(success_spec), rule_version, goal_instance_id))
        self.db.commit()

    def close_goal(self, goal_instance_id, status, at, judgement):
        assert status in ("succeeded", "failed", "inconclusive", "abandoned"), status
        self._append_history(goal_instance_id, {"at": at, "change": status})
        self.db.execute("UPDATE goals SET status=?, closed_at=?, judgement=? WHERE goal_instance_id=?",
                        (status, at, _j(judgement), goal_instance_id))
        self.db.commit()

    # ------------------------------------------------------------ decisions / actions / learnings
    def decision_for_trigger(self, trigger_event_id):
        r = self.db.execute("SELECT record FROM decisions WHERE trigger_event_id=?", (trigger_event_id,)).fetchone()
        return _uj(r["record"]) if r else None

    def save_decision(self, rec):
        self.db.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                        (rec["decision_id"], rec["viewer_id"], (rec.get("trigger") or {}).get("event_id"),
                         rec.get("status"), rec["created_at"], _j(rec)))
        self.db.commit()

    def decisions_for(self, user_id):
        return [_uj(r["record"]) for r in self.db.execute(
            "SELECT record FROM decisions WHERE user_id=? ORDER BY created_at", (user_id,)).fetchall()]

    def save_action(self, a):
        self.db.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?)",
                        (a["action_id"], a["decision_id"], a["user_id"], a["type"], a.get("published_at"),
                         a.get("send_at"), _j(a)))
        self.db.commit()

    def actions_for(self, user_id):
        return [_uj(r["record"]) for r in self.db.execute(
            "SELECT record FROM actions WHERE user_id=? ORDER BY published_at", (user_id,)).fetchall()]

    def add_learning(self, user_id, goal_instance_id, at, note):
        self.db.execute("INSERT INTO learnings(user_id, goal_instance_id, at, note) VALUES (?,?,?,?)",
                        (user_id, goal_instance_id, at, _j(note)))
        self.db.commit()

    def learnings_for(self, user_id):
        return [{"goal_instance_id": r["goal_instance_id"], "at": r["at"], "note": _uj(r["note"])}
                for r in self.db.execute("SELECT * FROM learnings WHERE user_id=? ORDER BY id", (user_id,))]

    # ------------------------------------------------------------ audit view
    def viewer_snapshot(self, user_id):
        """Everything the agent holds about one viewer, for viewer_state/<user_id>.json."""
        n = self.db.execute("SELECT COUNT(*) c FROM events WHERE user_id=?", (user_id,)).fetchone()["c"]
        decisions = self.decisions_for(user_id)
        return {
            "viewer_id": user_id, "arm": self.arm(user_id), "event_count": n,
            "profile_versions": self.profile_versions(user_id),
            "goals": self.goals_for(user_id), "decisions": decisions,
            "facts_at_last_decision": self.facts_at(user_id, decisions[-1]["decision_id"]) if decisions else [],
            "actions": self.actions_for(user_id), "learnings": self.learnings_for(user_id),
        }
