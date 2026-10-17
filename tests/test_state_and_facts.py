"""Steps 2-3: durable state, idempotent ingestion, source precedence and conflicts."""
import sqlite3
import unittest

from helpers import NOW, FakePack, ev, load, new_store, profile

from agent.resolver import resolve


class StoreTests(unittest.TestCase):
    def test_duplicate_event_is_stored_once_and_counted(self):
        s = new_store()
        e = ev("U1", "app_open", "2026-10-01T10:00:00.000Z")
        self.assertTrue(s.ingest_event(e, "trigger"))
        self.assertFalse(s.ingest_event(dict(e, received_at="2026-10-01T10:05:00.000Z"), "trigger"))
        self.assertEqual(len(s.events_for("U1")), 1)
        self.assertEqual(s.event_copies(e["event_id"]), 2)
        self.assertEqual(s.events_for("U1")[0]["received_at"], "2026-10-01T10:00:00.000Z")

    def test_only_one_open_goal_per_viewer(self):
        s = new_store()
        g = {"goal_instance_id": "GI-1", "user_id": "U1", "goal_id": "G02", "started_at": NOW,
             "deadline": "2026-10-04T12:00:00.000Z", "success_spec": {"event": "episode_completed"},
             "rule_version": 1}
        s.create_goal(g)
        with self.assertRaises(sqlite3.IntegrityError):
            s.create_goal(dict(g, goal_instance_id="GI-2", goal_id="G05"))
        s.close_goal("GI-1", "failed", NOW, {"why": "test"})
        s.create_goal(dict(g, goal_instance_id="GI-2", goal_id="G05"))  # allowed once the first is closed
        self.assertEqual(s.open_goal("U1")["goal_instance_id"], "GI-2")

    def test_decision_is_unique_per_trigger(self):
        s = new_store()
        rec = {"decision_id": "D-1", "viewer_id": "U1", "trigger": {"event_id": "E1"}, "status": "PUBLISHED",
               "created_at": NOW}
        s.save_decision(rec)
        self.assertEqual(s.decision_for_trigger("E1")["decision_id"], "D-1")
        with self.assertRaises(sqlite3.IntegrityError):
            s.save_decision(dict(rec, decision_id="D-2"))


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.pack = FakePack()

    def test_three_dialect_sources_disagree_app_setting_wins_and_conflicts_stay_visible(self):
        s = new_store()
        load(s, [profile("U1", dialect="d1")])
        trig = ev("U1", "app_open", NOW, dialect="d2",
                  props={"deeplink": "ott://c/cmp009/d4/title/T3"})
        s.ingest_event(trig, "trigger")
        f = resolve(s, self.pack, "U1", NOW, trig).f("dialect")
        self.assertEqual(f.value, "d2")
        self.assertEqual(f.source, "app_setting")
        self.assertEqual({c["source"]: c["value"] for c in f.conflicts},
                         {"profile": "d1", "campaign_deeplink": "d4"})

    def test_dialect_without_catalogue_content_is_none_not_a_fallback(self):
        s = new_store()
        load(s, [profile("U1", dialect="d1")])
        trig = ev("U1", "app_open", NOW, dialect="d7")
        s.ingest_event(trig, "trigger")
        f = resolve(s, self.pack, "U1", NOW, trig).f("dialect")
        self.assertIsNone(f.value)
        self.assertEqual(f.conflicts[0]["value"], "d1")

    def test_purchase_after_snapshot_overrides_profile_status(self):
        s = new_store()
        buy = ev("U1", "purchase_completed", "2026-10-01T11:58:00.000Z",
                 props={"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False})
        load(s, [profile("U1", status="never")], [buy])
        v = resolve(s, self.pack, "U1", NOW)
        self.assertEqual(v.value("subscription_status"), "active")
        self.assertEqual(v.f("subscription_status").refs, [f"event:{buy['event_id']}"])
        self.assertEqual(v.f("subscription_status").conflicts[0]["value"], "never")
        self.assertTrue(v.value("ever_paid"))

    def test_events_received_after_decision_time_are_not_used(self):
        s = new_store()
        late = ev("U1", "purchase_completed", "2026-10-01T11:00:00.000Z", received="2026-10-01T12:02:00.000Z",
                  props={"is_trial": False})
        load(s, [profile("U1", status="never")], [late])
        self.assertEqual(resolve(s, self.pack, "U1", NOW).value("subscription_status"), "never")
        self.assertEqual(resolve(s, self.pack, "U1", "2026-10-01T12:03:00.000Z").value("subscription_status"),
                         "active")

    def test_duplicate_profile_rows_that_disagree_lower_confidence(self):
        s = new_store()
        load(s, [profile("U1", status="never", updated="2026-09-29T12:00:00.000Z"),
                 profile("U1", status="active", updated="2026-09-14T12:00:00.000Z")])
        v = resolve(s, self.pack, "U1", NOW)
        self.assertEqual(v.value("subscription_status"), "never")      # newest row is the default...
        self.assertEqual(v.f("subscription_status").confidence, "low")  # ...but it is not trusted
        self.assertEqual(v.f("ever_paid").confidence, "low")

    def test_opt_out_event_overrides_snapshot_consent(self):
        s = new_store()
        out = ev("U1", "notification_opt_out", "2026-09-30T10:00:00.000Z", props={"channel": "push"})
        load(s, [profile("U1", push=True)], [out])
        f = resolve(s, self.pack, "U1", NOW).f("consent_push")
        self.assertFalse(f.value)
        self.assertEqual(f.source, "event")

    def test_viewer_without_profile_has_no_consent(self):
        s = new_store()
        trig = ev("U9", "app_open", NOW)
        s.ingest_event(trig, "trigger")
        v = resolve(s, self.pack, "U9", NOW, trig)
        self.assertFalse(v.value("consent_push"))
        self.assertFalse(v.value("consent_whatsapp"))
        self.assertIsNone(v.value("signup_at"))

    def test_wrong_phone_clock_is_corrected_for_trigger_age(self):
        s = new_store()
        load(s, [profile("U1")])
        # phone clock 5 hours behind; really happened 9 s before delivery
        trig = ev("U1", "app_open", "2026-10-01T11:59:51.000Z", received=NOW, skew_s=5 * 3600)
        trig["deliver_at"] = NOW
        s.ingest_event(trig, "trigger")
        f = resolve(s, self.pack, "U1", NOW, trig).f("trigger_age_s")
        self.assertEqual(f.value, 9.0)
        self.assertEqual(f.confidence, "medium")

    def test_unfinished_series_counts_language_variants_as_one_title(self):
        s = new_store()
        load(s, [profile("U1")], [
            ev("U1", "play_started", "2026-09-20T10:00:00.000Z", props={"title_id": "T1", "episode_id": "show-a-d1-s1e1"}),
            ev("U1", "play_started", "2026-09-21T10:00:00.000Z", props={"title_id": "T1e", "episode_id": "show-a-d1-s1e2"}),
        ])
        series = resolve(s, self.pack, "U1", NOW).value("unfinished_series")
        self.assertEqual(len(series), 1)
        self.assertEqual(series[0]["furthest_episode"], 2)
        self.assertEqual(series[0]["next_episode_id"], "show-a-d1-s1e3")


if __name__ == "__main__":
    unittest.main()
