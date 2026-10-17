"""Steps 9-10: judging goals from attributable signals, lessons fed back, and the impact statistics."""
import unittest
from datetime import datetime, timedelta

from helpers import FakePack, ev, load, new_store, profile, registry

from agent import impact
from agent.agent import Agent
from agent.judge import feedback, judge_all, judge_goal, lesson
from agent.model import MockProvider
from agent.timeutil import IST, UTC

OBS_END = datetime(2026, 10, 16, tzinfo=IST)
START = "2026-10-01T06:00:00.000Z"


def goal(gid="G02", deadline="2026-10-04T06:00:00.000Z", status="open", rule_version=1, spec=None):
    reg = registry()
    return {"goal_instance_id": "GI-1", "user_id": "U1", "goal_id": gid, "status": status, "started_at": START,
            "deadline": deadline, "success_spec": spec or reg.success_spec(gid), "rule_version": rule_version,
            "history": [], "judgement": None}


def out_ev(event, at, **props):
    """An outcome event as the simulator writes it (trusted server time, no occurred_at_est)."""
    e = ev("U1", event, at, props=props)
    e.pop("_q")
    return e


PAGE = {"action_id": "A-1", "type": "home_page", "goal_instance": "GI-1"}
PUSH = {"action_id": "A-2", "type": "push", "goal_instance": "GI-1"}


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.pack = FakePack()

    def judge(self, g, events, actions=(PAGE,), arm="agent"):
        evs = [{**e, "occurred_at_est": e["received_at"]} for e in events]
        return judge_goal(g, arm, list(actions), evs, self.pack, OBS_END)

    def test_success_after_the_page_was_shown_is_attributed(self):
        o = self.judge(goal(), [out_ev("home_page_shown", "2026-10-01T06:00:10.000Z", personalised=True,
                                       page_action_id="A-1"),
                                out_ev("episode_completed", "2026-10-02T10:00:00.000Z", title_id="F1")])
        self.assertEqual((o["status"], o["attribution"]), ("succeeded", "success_after_exposure"))

    def test_published_but_never_shown_is_not_exposure(self):
        o = self.judge(goal(), [out_ev("episode_completed", "2026-10-02T10:00:00.000Z", title_id="F1")])
        self.assertEqual(o["attribution"], "success_without_exposure")
        self.assertTrue(any("never shown" in n for n in lesson(o)["notes"]))

    def test_success_after_the_deadline_is_a_failure(self):
        o = self.judge(goal(), [out_ev("episode_completed", "2026-10-05T10:00:00.000Z", title_id="F1")])
        self.assertEqual(o["status"], "failed")

    def test_window_past_observation_end_is_inconclusive(self):
        o = self.judge(goal(deadline="2026-10-20T06:00:00.000Z"), [])
        self.assertEqual(o["status"], "inconclusive")

    def test_abandoned_goal_is_inconclusive_not_failed(self):
        g = goal(status="abandoned")
        g["judgement"] = {"why": "no longer an active subscriber"}
        self.assertEqual(self.judge(g, [])["status"], "inconclusive")

    def test_holdout_is_judged_the_same_way(self):
        o = self.judge(goal(), [out_ev("episode_completed", "2026-10-02T10:00:00.000Z", title_id="F1")],
                       actions=(), arm="holdout")
        self.assertEqual((o["status"], o["attribution"]), ("succeeded", "holdout"))

    def test_corrected_g03_rule_is_what_the_goal_is_judged_on(self):
        reg = registry()
        reg.apply_correction({"at": "2026-10-01T08:30:00.000Z", "goal_id": "G03",
                              "old_success": {"event": "app_open"}, "new_success": {"event": "play_started"}})
        g = goal("G03", "2026-10-03T06:00:00.000Z", rule_version=2, spec=reg.success_spec("G03"))
        o = self.judge(g, [out_ev("app_open", "2026-10-02T06:00:00.000Z")])
        self.assertEqual((o["status"], o["rule_version"]), ("failed", 2))
        o = self.judge(g, [out_ev("play_started", "2026-10-02T06:00:00.000Z", title_id="F1")])
        self.assertEqual(o["status"], "succeeded")


class LessonTests(unittest.TestCase):
    def test_lessons_change_the_next_decision(self):
        store = new_store()
        store.set_arms({"U1": "agent"})
        load(store, [profile("U1", status="lapsed")], [
            ev("U1", "subscription_lapsed", "2026-09-21T10:00:00.000Z"),
            ev("U1", "episode_completed", "2026-09-20T10:00:00.000Z", props={"title_id": "F3"})])
        agent = Agent(store, FakePack(), registry(), MockProvider())
        t = ev("U1", "app_open", START)
        t["deliver_at"] = START
        first = agent.handle_trigger(t, START)
        self.assertEqual((first["goal"]["id"], first["action"]), ("G06", "HOME_PAGE+PUSH"))
        page_id, push_id = first["action_ids"]
        seen = out_ev("home_page_shown", "2026-10-01T06:00:10.000Z", personalised=True, page_action_id=page_id)
        failed_push = out_ev("message_failed", "2026-10-02T13:30:00.000Z", channel="push",
                             reason="token_invalid", action_id=push_id)
        judge_all(store, agent.pack, [seen, failed_push], OBS_END, "2026-10-16T00:00:00.000Z")
        fb = feedback(store, "U1", "2026-10-20T06:00:00.000Z")
        self.assertFalse(fb["push_ok"])                         # dead token: no push next time
        self.assertEqual(fb["avoid_goals"], ["G06"])            # failed although the page was seen
        t2 = ev("U1", "app_open", "2026-10-20T06:00:00.000Z")
        t2["deliver_at"] = t2["received_at"]
        second = agent.handle_trigger(t2, t2["deliver_at"])
        self.assertNotEqual(second["goal"]["id"], "G06")
        self.assertNotIn("PUSH", second["action"])
        self.assertEqual(second["lessons_applied"], fb)


class StatsTests(unittest.TestCase):
    def test_difference_of_proportions(self):
        d = impact.diff(60, 500, 50, 500)
        self.assertEqual(d["diff_pts"], 2.0)
        self.assertLess(d["ci95_pts"][0], 0)
        self.assertGreater(d["p_value"], 0.3)
        self.assertLess(impact.diff(150, 500, 50, 500)["p_value"], 0.001)

    def rec(self, u, arm, ok, served=False, exposed=False):
        return {"viewer_id": u, "arm": arm, "goal_id": "G02", "started_at": START,
                "status": "succeeded" if ok else "failed",
                "attribution": "x", "exposure": {"published": ["home_page"] if served else [],
                                                 "first_exposure_at": START if exposed else None}}

    def test_itt_uses_first_goal_per_viewer_and_the_randomized_arm(self):
        rows = [self.rec("a1", "agent", True), self.rec("a2", "agent", False),
                self.rec("h1", "holdout", False), self.rec("h2", "holdout", False),
                {**self.rec("a2", "agent", True), "started_at": "2026-10-09T00:00:00.000Z"}]   # later goal ignored
        d = impact.itt(rows)
        self.assertEqual((d["rate_1"], d["n_1"], d["rate_0"]), (0.5, 2, 0.0))

    def test_served_vs_rest_shows_a_gap_that_the_arms_do_not(self):
        """Selection: the agent only serves active viewers, who succeed more anyway. No real effect here."""
        rows = []
        for i in range(200):
            active = i % 2 == 0
            rows.append(self.rec(f"a{i}", "agent", active and i % 4 == 0, served=active, exposed=active))
            rows.append(self.rec(f"h{i}", "holdout", active and i % 4 == 0))
        self.assertEqual(impact.itt(rows)["diff_pts"], 0.0)
        self.assertGreater(impact.misleading(rows)["served_vs_everyone_else (pilot method)"]["diff_pts"], 20)

    def test_ground_truth_flags_leakage_into_holdout(self):
        with_a = [self.rec("a", "agent", True), self.rec("h", "holdout", True)]
        without = [self.rec("a", "agent", False), self.rec("h", "holdout", False)]
        gt = impact.ground_truth(with_a, without)
        self.assertEqual(gt["agent"]["viewers_gained"], 1)
        self.assertEqual(gt["holdout"]["viewers_gained"], 1)   # a real run must show 0 here


if __name__ == "__main__":
    unittest.main()


class EstimatorTests(unittest.TestCase):
    def units(self, n=400, effect=0.1):
        """Outcome driven mostly by a pre-period covariate, plus a known treatment effect."""
        rows, cov = [], {}
        for i in range(n):
            arm = "agent" if i % 2 else "holdout"
            active = (i * 7919) % 10                      # deterministic spread of prior activity
            p = active / 12 + (effect if arm == "agent" else 0)
            ok = ((i * 104729) % 1000) / 1000 < p
            rows.append({"viewer_id": f"u{i}", "arm": arm, "goal_id": "G02" if i % 3 else "G07", "started_at": START,
                         "status": "succeeded" if ok else "failed", "attribution": "x",
                         "exposure": {"published": [], "first_exposure_at": None}})
            cov[f"u{i}"] = {"active_days": active, "play_days": 0, "completions": 0, "ever_paid": 0, "days_known": 30}
        return rows, cov

    def test_cuped_reduces_variance_when_history_predicts_the_outcome(self):
        rows, cov = self.units()
        a = impact.adjusted(rows, cov)
        self.assertGreater(a["variance_reduction"], 0.2)
        self.assertLess(a["cuped"]["se_pts"], a["plain"]["se_pts"])

    def test_estimators_agree_on_the_question(self):
        rows, cov = self.units(effect=0.15)
        a = impact.adjusted(rows, cov)
        for k in ("plain", "cuped", "stratified_by_goal", "cuped_and_stratified"):
            self.assertGreater(a[k]["diff_pts"], 5, k)


class PushCopyTests(unittest.TestCase):
    def test_each_goal_names_a_title_its_copy_is_true_for(self):
        from agent.actions import _message_title, Catalog
        from agent.goals import GoalChoice
        from agent.resolver import resolve
        pack = FakePack()
        pack.titles["F5"]["content_type"] = "micro_drama"
        store = new_store()
        load(store, [profile("U1")])
        v = resolve(store, pack, "U1", START)
        cat = Catalog(pack)
        tid, kind = _message_title(v, GoalChoice("G02", success={}), pack, cat, "d1", "2026-10-02T13:30:00.000Z")
        self.assertEqual((pack.titles[tid]["content_type"], kind), ("micro_drama", "short"))
        tid, kind = _message_title(v, GoalChoice("G08", success={}), pack, cat, "d1", "2026-10-02T13:30:00.000Z")
        self.assertEqual((tid, kind), ("T4", "new"))                       # the only recent release
        tid, kind = _message_title(v, GoalChoice("G05", success={}), pack, cat, "d1", "2026-10-02T13:30:00.000Z")
        self.assertTrue(pack.titles[tid]["is_premium"])
