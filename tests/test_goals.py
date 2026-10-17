"""Step 4: eligibility, choice, single active plan, and the corrected goal rule."""
import unittest

from helpers import NOW, FakePack, ev, load, new_store, profile, registry

from agent.goals import apply_rule_correction, choose_goal, eligibility, success_event
from agent.resolver import resolve

G03_FIX = {"at": "2026-10-01T08:30:00.000Z", "type": "goal_rule_corrected", "goal_id": "G03",
           "old_success": {"event": "app_open"}, "new_success": {"event": "play_started"},
           "note": "an app open alone was never meant to count as a return"}


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.pack, self.reg = FakePack(), registry()

    def view(self, profiles=(), events=(), user="U1", now=NOW):
        s = new_store()
        load(s, profiles, events)
        return s, resolve(s, self.pack, user, now)

    def test_unmeasurable_goals_are_never_eligible(self):
        _, v = self.view([profile("U1", status="active")])
        e = eligibility(v, self.reg, self.pack)
        self.assertFalse(e["G10"].eligible)
        self.assertFalse(e["G11"].eligible)

    def test_new_user_without_plays_gets_first_play_goal_with_evidence(self):
        sign = ev("U1", "signup_completed", "2026-09-30T09:00:00.000Z")
        _, v = self.view([profile("U1", signup="2026-09-30T09:00:00.000Z")], [sign])
        g = choose_goal(v, self.reg, self.pack)
        self.assertEqual(g.goal_id, "G01")
        self.assertIn(f"event:{sign['event_id']}", g.evidence)
        self.assertEqual(g.deadline, "2026-10-02T12:00:00.000Z")

    def test_conflicting_status_blocks_payment_goals(self):
        comp = ev("U1", "episode_completed", "2026-09-20T10:00:00.000Z",
                  props={"title_id": "T2", "episode_id": "film-b-s1e1"})
        _, v = self.view([profile("U1", status="never", updated="2026-09-29T12:00:00.000Z"),
                          profile("U1", status="active", updated="2026-09-14T12:00:00.000Z")], [comp])
        g = choose_goal(v, self.reg, self.pack)
        self.assertNotEqual(g.goal_id, "G05")
        g05 = next(c for c in g.considered if c["goal_id"] == "G05")
        self.assertTrue(g05.get("blocked_by_low_confidence"))

    def test_open_goal_is_kept_not_replaced(self):
        s, v = self.view([profile("U1")])
        s.create_goal({"goal_instance_id": "GI-1", "user_id": "U1", "goal_id": "G05", "started_at": NOW,
                       "deadline": "2026-10-08T12:00:00.000Z", "success_spec": self.reg.success_spec("G05"),
                       "rule_version": 1})
        g = choose_goal(v, self.reg, self.pack, open_goal=s.open_goal("U1"))
        self.assertEqual((g.goal_id, g.continuing), ("G05", "GI-1"))

    def test_g03_correction_touches_only_open_g03_goals_and_changes_success(self):
        s = new_store()
        mk = lambda gi, u, gid: s.create_goal({
            "goal_instance_id": gi, "user_id": u, "goal_id": gid, "started_at": "2026-10-01T05:00:00.000Z",
            "deadline": "2026-10-03T05:00:00.000Z", "success_spec": self.reg.success_spec(gid), "rule_version": 1})
        mk("GI-a", "U1", "G03")
        mk("GI-b", "U2", "G02")
        mk("GI-c", "U3", "G03")
        s.close_goal("GI-c", "failed", "2026-10-01T06:00:00.000Z", {})
        affected = apply_rule_correction(s, self.reg, G03_FIX)
        self.assertEqual(affected, ["GI-a"])
        self.assertEqual(s.goal("GI-b")["rule_version"], 1)
        self.assertEqual(s.goal("GI-c")["success_spec"]["event"], "app_open")  # closed goals keep their rule
        g = s.goal("GI-a")
        self.assertEqual((g["success_spec"]["event"], g["rule_version"]), ("play_started", 2))
        next_day_open = ev("U1", "app_open", "2026-10-02T06:00:00.000Z")
        next_day_play = ev("U1", "play_started", "2026-10-02T06:05:00.000Z", props={"title_id": "T2"})
        self.assertIsNone(success_event(g, [next_day_open], self.pack))
        self.assertEqual(success_event(g, [next_day_open, next_day_play], self.pack), next_day_play)

    def test_g03_same_ist_day_does_not_count(self):
        g = {"goal_id": "G03", "started_at": "2026-10-01T05:00:00.000Z", "deadline": "2026-10-03T05:00:00.000Z",
             "success_spec": self.reg.success_spec("G03")}
        same_day = ev("U1", "app_open", "2026-10-01T18:00:00.000Z")   # 23:30 IST, still Oct 1
        next_day = ev("U1", "app_open", "2026-10-01T19:00:00.000Z")   # 00:30 IST Oct 2
        self.assertIsNone(success_event(g, [same_day], self.pack))
        self.assertEqual(success_event(g, [same_day, next_day], self.pack), next_day)

    def test_trial_purchase_is_not_paid_success(self):
        g = {"goal_id": "G05", "started_at": NOW, "deadline": "2026-10-08T12:00:00.000Z",
             "success_spec": self.reg.success_spec("G05")}
        trial = ev("U1", "purchase_completed", "2026-10-02T10:00:00.000Z", props={"is_trial": True})
        self.assertIsNone(success_event(g, [trial], self.pack))


if __name__ == "__main__":
    unittest.main()
