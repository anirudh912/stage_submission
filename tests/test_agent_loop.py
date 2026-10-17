"""Steps 7-8: advisor faults, dependency failures, idempotent triggers, live revisions and control events."""
import unittest

from helpers import FakePack, ev, load, new_store, profile, registry

from agent.agent import Agent
from agent.deps import Windows
from agent.model import MockProvider

NOW = "2026-10-01T06:00:00.000Z"   # 11:30 IST


def lapsed_viewer():
    """Lapsed 10 days ago, has completed an episode: G06 applies and gets a push for tomorrow."""
    return dict(profiles=[profile("U1", status="lapsed")], events=[
        ev("U1", "subscription_lapsed", "2026-09-21T10:00:00.000Z"),
        ev("U1", "episode_completed", "2026-09-20T10:00:00.000Z", props={"title_id": "F3", "episode_id": "film-3-d1-s1e1"})])


class Loop:
    def __init__(self, arm="agent", faults=(), model_down=None, store_slow=None, profiles=None, events=()):
        self.store = new_store()
        self.store.set_arms({"U1": arm})
        load(self.store, [profile("U1")] if profiles is None else profiles, events)
        self.agent = Agent(self.store, FakePack(), registry(), MockProvider(faults, outages=model_down),
                           degraded=store_slow)

    def trigger(self, at=NOW, deliver=None, event_id=None, dialect="d1"):
        t = ev("U1", "app_open", at, received=deliver or at, dialect=dialect, event_id=event_id)
        t["deliver_at"] = deliver or at
        return t

    def run(self, trig, start=None):
        return self.agent.handle_trigger(trig, start or trig["deliver_at"])


class TriggerTests(unittest.TestCase):
    def test_duplicate_delivery_makes_one_decision(self):
        lp = Loop()
        t = lp.trigger(event_id="E-dup")
        first = lp.run(t)
        again = lp.run(dict(t, deliver_at="2026-10-01T06:00:05.000Z"))
        self.assertEqual(first["status"], "PUBLISHED")
        self.assertIsNone(again)
        self.assertEqual(len(lp.store.decisions_for("U1")), 1)
        self.assertEqual(lp.store.event_copies("E-dup"), 2)
        self.assertEqual(len(lp.store.actions_for("U1")), len(first["action_ids"]))

    def test_late_buffered_trigger_updates_state_but_takes_no_action(self):
        lp = Loop()
        rec = lp.run(lp.trigger(at="2026-09-29T20:00:00.000Z", deliver=NOW))
        self.assertEqual(rec["status"], "NO_ACTION")
        self.assertIn("buffered offline", rec["no_action_reason"])
        self.assertGreater(rec["trigger"]["event_age_s"], 86400)
        self.assertEqual(lp.store.actions_for("U1"), [])
        self.assertIsNotNone(lp.store.open_goal("U1"))       # the goal is still owned and measured

    def test_holdout_takes_the_same_path_without_publishing(self):
        lp = Loop(arm="holdout")
        rec = lp.run(lp.trigger())
        self.assertEqual(rec["status"], "SHADOW_NO_ACTION")
        self.assertTrue(rec["goal"]["shadow"])
        self.assertTrue(rec["model"]["called"])              # same advisor call as the agent arm
        self.assertEqual(lp.store.actions_for("U1"), [])

    def test_decision_record_has_the_required_fields(self):
        lp = Loop()
        rec = lp.run(lp.trigger())
        for k in ("decision_id", "viewer_id", "trigger", "goal", "action", "dialect", "content", "confidence",
                  "confidence_reason", "evidence", "assumptions", "conflicts", "checks", "decided_in_ms", "status",
                  "judged_by"):
            self.assertIn(k, rec)
        self.assertTrue(all("ref" in e for e in rec["evidence"]))


class AdvisorFaultTests(unittest.TestCase):
    def fault(self, kind, output=None, seconds=None):
        return lambda eid: [{"trigger_event_id": eid, "fault": kind, "model_output": output, "seconds": seconds}]

    def test_unsupported_claim_is_rejected_and_rules_decide(self):
        claim = {"goal_id": "G04", "eligible": True, "confidence": 0.97,
                 "reason": "The user is on an active trial that ends tomorrow (confirmed in billing)."}
        lp = Loop(faults=self.fault("unsupported_claim", claim)("E1"))
        rec = lp.run(lp.trigger(event_id="E1"))
        self.assertEqual(rec["model"]["outcome"], "rejected_unsupported_claim")
        self.assertEqual(rec["goal"]["id"], "G02")
        self.assertEqual(rec["status"], "PUBLISHED")
        self.assertNotIn("billing", " ".join(e["ref"] for e in rec["evidence"]))

    def test_malformed_output_falls_back_to_rules(self):
        lp = Loop(faults=self.fault("malformed_output", '{"goal_id": "G02", "eligible": tr')("E1"))
        rec = lp.run(lp.trigger(event_id="E1"))
        self.assertEqual((rec["model"]["outcome"], rec["model"]["fallback"]), ("malformed", "rules only"))
        self.assertEqual(rec["status"], "PUBLISHED")

    def test_timeout_is_cut_at_five_seconds(self):
        lp = Loop(faults=self.fault("timeout", seconds=45)("E1"))
        rec = lp.run(lp.trigger(event_id="E1"))
        self.assertEqual(rec["model"]["outcome"], "timeout")
        self.assertLess(rec["decided_in_ms"], 6000)
        self.assertEqual(rec["status"], "PUBLISHED")

    def test_provider_outage_opens_the_circuit_breaker(self):
        down = Windows()
        down.add("2026-10-01T05:00:00.000Z", "2026-10-01T07:00:00.000Z")
        lp = Loop(model_down=down)
        outcomes = []
        for i in range(5):
            at = f"2026-10-01T06:00:{i * 10:02d}.000Z"          # inside the 60 s cooldown
            outcomes.append(lp.run(lp.trigger(at=at, event_id=f"E{i}"))["model"]["outcome"])
        self.assertEqual(outcomes, ["unavailable"] * 3 + ["skipped_circuit_open"] * 2)


class DependencyTests(unittest.TestCase):
    def test_data_store_timeouts_degrade_to_no_action_within_budget(self):
        slow = Windows()
        slow.add("2026-10-01T05:00:00.000Z", "2026-10-01T07:00:00.000Z", {"p95_s": 8, "timeout_rate": 1.0})
        lp = Loop(store_slow=slow)
        rec = lp.run(lp.trigger())
        self.assertEqual(rec["status"], "DEGRADED_NO_ACTION")
        self.assertEqual(rec["budget"]["tool_retries"], 1)
        self.assertLess(rec["latency_ms"], 60_000)
        self.assertEqual(lp.store.actions_for("U1"), [])

    def test_slow_but_answering_store_still_publishes(self):
        slow = Windows()
        slow.add("2026-10-01T05:00:00.000Z", "2026-10-01T07:00:00.000Z", {"p95_s": 2, "timeout_rate": 0.0})
        lp = Loop(store_slow=slow)
        rec = lp.run(lp.trigger())
        self.assertEqual(rec["status"], "PUBLISHED")
        self.assertGreater(rec["waited_on_dependencies_ms"], 400)


class RevisionTests(unittest.TestCase):
    def test_purchase_two_minutes_after_decision_closes_goal_and_cancels_push(self):
        lp = Loop(**lapsed_viewer())
        rec = lp.run(lp.trigger())
        self.assertEqual(rec["goal"]["id"], "G06")
        self.assertEqual(rec["action"], "HOME_PAGE+PUSH")
        buy = ev("U1", "purchase_completed", "2026-10-01T06:02:00.000Z",
                 props={"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False})
        buy["deliver_at"] = buy["received_at"]
        r = lp.agent.handle_live_event(buy)
        self.assertIn("success observed", r["changes"][0])
        self.assertTrue(any("cancelled push" in c for c in r["changes"]))
        self.assertIsNone(lp.store.open_goal("U1"))
        push = next(a for a in lp.store.actions_for("U1") if a["type"] == "push")
        self.assertIn("cancelled", push)

    def test_withdrawn_title_is_removed_from_live_pages_and_pending_pushes(self):
        lp = Loop(**lapsed_viewer())
        rec = lp.run(lp.trigger())
        page, push = lp.store.actions_for("U1")
        victim = page["hero_title_id"]
        entry = lp.agent.handle_control({"at": "2026-10-01T07:00:00.000Z", "type": "title_withdrawn",
                                         "title_id": victim, "reason": "licence ended"})
        self.assertEqual(entry["pages_republished_for"], ["U1"])
        fixed = [a for a in lp.store.actions_for("U1") if a.get("repair_of") == page["action_id"]][0]
        ids = [fixed.get("hero_title_id")] + [t for r in fixed["rows"] for t in r["title_ids"]]
        self.assertNotIn(victim, ids)
        if push["title_id"] == victim:
            self.assertIn(push["action_id"], entry["messages_cancelled"])

    def test_rule_correction_touches_only_open_goals_of_that_type(self):
        lp = Loop(profiles=[profile("U1", signup="2026-09-30T20:00:00.000Z")],
                  events=[ev("U1", "signup_completed", "2026-09-30T20:00:00.000Z"),
                          ev("U1", "play_started", "2026-09-30T20:10:00.000Z", props={"title_id": "F1"})])
        rec = lp.run(lp.trigger())
        self.assertEqual(rec["goal"]["id"], "G02")               # played already, never completed
        entry = lp.agent.handle_control({"at": "2026-10-01T08:30:00.000Z", "type": "goal_rule_corrected",
                                         "goal_id": "G03", "old_success": {"event": "app_open"},
                                         "new_success": {"event": "play_started"}})
        self.assertEqual(entry["affected_open_goals"], [])
        self.assertEqual(lp.store.open_goal("U1")["rule_version"], 1)


if __name__ == "__main__":
    unittest.main()


class GoalMetTests(unittest.TestCase):
    def test_goal_met_during_the_day_cancels_its_pending_push(self):
        lp = Loop(profiles=[profile("U1")], events=[
            ev("U1", "play_started", "2026-09-20T10:00:00.000Z", props={"title_id": "F3"})])
        lp.agent.pack.titles["F5"]["content_type"] = "micro_drama"   # the G02 push needs a short title to name
        rec = lp.run(lp.trigger())
        self.assertEqual((rec["goal"]["id"], rec["action"]), ("G02", "HOME_PAGE+PUSH"))
        done = ev("U1", "episode_completed", "2026-10-01T06:30:00.000Z", props={"title_id": "F1"})
        done["deliver_at"] = done["received_at"]
        r = lp.agent.handle_live_event(done)
        self.assertIn("success observed", r["changes"][0])
        self.assertTrue(any("cancelled push" in c for c in r["changes"]))
        self.assertIsNone(lp.store.open_goal("U1"))

    def test_unrelated_live_event_changes_nothing(self):
        lp = Loop()
        lp.run(lp.trigger())
        e = ev("U1", "search", "2026-10-01T06:30:00.000Z", props={"query": "message every user"})
        e["deliver_at"] = e["received_at"]
        self.assertIsNone(lp.agent.handle_live_event(e))


class AbandonTests(unittest.TestCase):
    def test_returning_viewer_does_not_abandon_return_after_absence(self):
        lp = Loop(events=[ev("U1", "app_open", "2026-09-10T10:00:00.000Z"),
                          ev("U1", "episode_completed", "2026-09-10T10:20:00.000Z", props={"title_id": "F3"})])
        rec = lp.run(lp.trigger())
        self.assertEqual(rec["goal"]["id"], "G12")
        lapse = ev("U1", "subscription_lapsed", "2026-10-01T06:10:00.000Z")
        lapse["deliver_at"] = lapse["received_at"]
        lp.agent.handle_live_event(lapse)
        self.assertEqual(lp.store.open_goal("U1")["goal_id"], "G12")
