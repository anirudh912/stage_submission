"""The four scenarios the brief requires, each end to end through the agent loop.

  1. a late or duplicate trigger      -> test_1_*
  2. a conflicting source             -> test_2_*
  3. a tool failure                   -> test_3_*
  4. a corrected goal rule            -> test_4_*

More focused tests for each part live in the other test files (see tests/README.md).
"""
import unittest
from datetime import datetime

from helpers import FakePack, ev, load, new_store, profile, registry

from agent.agent import Agent
from agent.deps import Windows
from agent.judge import judge_all
from agent.model import MockProvider
from agent.timeutil import IST

NOW = "2026-10-01T06:00:00.000Z"    # 11:30 IST


def agent_for(profiles=None, events=(), arm="agent", **kw):
    store = new_store()
    store.set_arms({"U1": arm})
    load(store, [profile("U1")] if profiles is None else profiles, events)
    return store, Agent(store, FakePack(), registry(), MockProvider(kw.pop("faults", ()), outages=kw.pop("model_down", None)),
                        degraded=kw.pop("store_slow", None))


def trigger(at=NOW, deliver=None, event_id=None, dialect="d1", props=None):
    t = ev("U1", "app_open", at, received=deliver or at, dialect=dialect, event_id=event_id, props=props)
    t["deliver_at"] = deliver or at
    return t


class RequiredScenarios(unittest.TestCase):
    # ---------------------------------------------------------------- 1. late or duplicate trigger
    def test_1a_duplicate_trigger_is_idempotent(self):
        store, agent = agent_for()
        t = trigger(event_id="E-1")
        first = agent.handle_trigger(t, t["deliver_at"])
        second = agent.handle_trigger(dict(t, deliver_at="2026-10-01T06:00:03.000Z"), "2026-10-01T06:00:03.000Z")
        self.assertEqual(first["status"], "PUBLISHED")
        self.assertIsNone(second)                                       # no second decision
        self.assertEqual(len(store.decisions_for("U1")), 1)
        self.assertEqual(len(store.actions_for("U1")), len(first["action_ids"]))   # no second page or push
        self.assertEqual(store.event_copies("E-1"), 2)                  # but the redelivery is counted

    def test_1b_late_buffered_trigger_does_not_act_as_if_the_viewer_were_here(self):
        store, agent = agent_for()
        t = trigger(at="2026-09-28T15:00:00.000Z", deliver=NOW)         # buffered offline for ~2.6 days
        rec = agent.handle_trigger(t, t["deliver_at"])
        self.assertEqual(rec["status"], "NO_ACTION")
        self.assertIn("buffered offline", rec["no_action_reason"])
        self.assertEqual(store.actions_for("U1"), [])
        self.assertEqual(len(store.events_for("U1")), 1)                # state is still updated

    # ---------------------------------------------------------------- 2. conflicting source
    def test_2_three_dialect_sources_disagree_and_a_purchase_beats_the_snapshot(self):
        buy = ev("U1", "purchase_completed", "2026-10-01T05:58:00.000Z",
                 props={"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False})
        store, agent = agent_for(profiles=[profile("U1", status="never", dialect="d2")], events=[buy])
        t = trigger(props={"deeplink": "ott://c/cmp009/d4/title/T3"})   # app d1, profile d2, campaign d4
        rec = agent.handle_trigger(t, t["deliver_at"])
        self.assertEqual((rec["dialect"]["used"], rec["dialect"]["source"]), ("d1", "app_setting"))
        self.assertEqual(sorted(rec["dialect"]["conflicts"]), ["campaign_deeplink:d4", "profile:d2"])
        page = store.actions_for("U1")[0]
        ids = [page.get("hero_title_id")] + [x for r in page["rows"] for x in r["title_ids"]]
        self.assertTrue(all(agent.pack.titles[i]["dialect"] == "d1" for i in ids if i))
        status = next(c for c in rec["conflicts"] if c["fact"] == "subscription_status")
        self.assertEqual((status["used"], status["source"]), ("active", "event"))   # event beats stale profile
        self.assertNotEqual(rec["goal"]["id"], "G05")                   # no 'first paid plan' for a payer

    # ---------------------------------------------------------------- 3. tool failure
    def test_3a_model_provider_down_falls_back_to_rules(self):
        down = Windows()
        down.add("2026-10-01T05:00:00.000Z", "2026-10-01T07:00:00.000Z")
        store, agent = agent_for(model_down=down)
        rec = agent.handle_trigger(trigger(), NOW)
        self.assertEqual((rec["model"]["outcome"], rec["model"]["fallback"]), ("unavailable", "rules only"))
        self.assertEqual(rec["status"], "PUBLISHED")                    # the decision still happens, on rules
        self.assertTrue(rec["within_budget"])

    def test_3b_data_store_timeouts_degrade_to_a_safe_no_action(self):
        slow = Windows()
        slow.add("2026-10-01T05:00:00.000Z", "2026-10-01T07:00:00.000Z", {"p95_s": 8, "timeout_rate": 1.0})
        store, agent = agent_for(store_slow=slow)
        rec = agent.handle_trigger(trigger(), NOW)
        self.assertEqual(rec["status"], "DEGRADED_NO_ACTION")
        self.assertLess(rec["latency_ms"], 60_000)                      # gives up inside the latency budget
        self.assertEqual(store.actions_for("U1"), [])

    # ---------------------------------------------------------------- 4. corrected goal rule
    def test_4_corrected_g03_rule_repoints_only_open_g03_goals_and_judging_uses_it(self):
        # new viewer who already played and finished an episode: G03 (second-day return) is the goal
        signup = ev("U1", "signup_completed", "2026-09-30T20:00:00.000Z")
        done = [ev("U1", "play_started", "2026-09-30T20:10:00.000Z", props={"title_id": "F1"}),
                ev("U1", "episode_completed", "2026-09-30T20:40:00.000Z", props={"title_id": "F1"})]
        store, agent = agent_for(profiles=[profile("U1", signup="2026-09-30T20:00:00.000Z")], events=[signup] + done)
        rec = agent.handle_trigger(trigger(), NOW)
        self.assertEqual((rec["goal"]["id"], rec["goal"]["success_signal"]["event"]), ("G03", "app_open"))
        entry = agent.handle_control({"at": "2026-10-01T08:30:00.000Z", "type": "goal_rule_corrected",
                                      "goal_id": "G03", "old_success": {"event": "app_open"},
                                      "new_success": {"event": "play_started"},
                                      "note": "an app open alone was never meant to count as a return"})
        self.assertEqual(entry["affected_open_goals"], [rec["goal"]["instance"]])
        g = store.open_goal("U1")
        self.assertEqual((g["success_spec"]["event"], g["rule_version"]), ("play_started", 2))
        self.assertEqual(g["history"][-1]["change"], "success_rule_corrected")
        # next day: an app open alone no longer counts; a play does
        obs_end = datetime(2026, 10, 16, tzinfo=IST)
        only_open = [{**ev("U1", "app_open", "2026-10-02T06:00:00.000Z"), "occurred_at_est": None}]
        out = judge_all(store, agent.pack, only_open, obs_end, "2026-10-16T00:00:00.000Z")
        self.assertEqual((out[0]["status"], out[0]["rule_version"]), ("failed", 2))


if __name__ == "__main__":
    unittest.main()
