"""Steps 5-6: the action builder (including doing nothing) and the independent verifier."""
import unittest

from helpers import FakePack, ev, load, new_store, profile, registry

from agent.actions import Catalog, choose_actionable_goal, plan
from agent.resolver import resolve
from agent.verifier import Verifier

NOW = "2026-10-01T06:00:00.000Z"        # 11:30 IST


def series_events(user, dialect_titles=("T1",)):
    """Viewer watched episodes 1-2 of show-a-d1 ten days ago, so G07 applies (and G02 does not)."""
    out = []
    for tid in dialect_titles:
        out += [ev(user, "play_started", "2026-09-21T10:00:00.000Z", props={"title_id": tid, "episode_id": "show-a-d1-s1e1"}),
                ev(user, "episode_completed", "2026-09-21T10:20:00.000Z", props={"title_id": tid, "episode_id": "show-a-d1-s1e1"}),
                ev(user, "play_started", "2026-09-21T10:30:00.000Z", props={"title_id": tid, "episode_id": "show-a-d1-s1e2"})]
    return out


class Harness:
    def __init__(self, arm="agent", profiles=None, events=(), user="U1"):
        self.pack, self.reg = FakePack(), registry()
        self.store, self.user = new_store(), user
        self.cat = Catalog(self.pack)
        self.store.set_arms({user: arm})
        load(self.store, [profile(user)] if profiles is None else profiles, events)
        self.ver = Verifier(self.store, self.cat, self.pack, self.reg)

    def trigger(self, at=NOW, dialect="d1", deliver=None, **kw):
        t = ev(self.user, "app_open", at, received=deliver or at, dialect=dialect, **kw)
        t["deliver_at"] = deliver or at
        self.store.ingest_event(t, "trigger")
        return t

    def decide(self, trig):
        now = trig["deliver_at"]
        v = resolve(self.store, self.pack, self.user, now, trig)
        g = choose_actionable_goal(v, self.reg, self.pack, self.cat, now, self.store.open_goal(self.user))
        p = plan(v, g, self.pack, self.cat, self.store, now, self.store.arm(self.user))
        gd = {"goal_id": g.goal_id, "evidence": g.evidence, "success": g.success, "continuing": g.continuing}
        approved, checks = self.ver.verify(self.user, trig, now, gd, p["actions"], self.store.open_goal(self.user))
        return g, p, approved, checks

    def publish(self, actions, at=NOW):
        for i, a in enumerate(actions):
            self.store.save_action({**a, "action_id": f"A-{i}-{a['type']}", "decision_id": "D-x",
                                    "user_id": self.user, "published_at": at})

    def check(self, actions, goal=None, trig=None, budget=None):
        trig = trig or self.trigger()
        goal = goal or {"goal_id": "G02", "evidence": [f"event:{trig['event_id']}", "goal_rule:G02-elig-v1"]}
        return self.ver.verify(self.user, trig, trig["deliver_at"], goal, actions, budget=budget)


def page(rows, dialect="d1", hero=None):
    p = {"type": "home_page", "dialect": dialect, "rows": [{"row_type": "popular_in_dialect", "title_ids": r}
                                                            for r in rows]}
    if hero:
        p["hero_title_id"] = hero
    return p


def failed(checks):
    return {f["check"] for f in checks["failed"]}


GOOD_ROW = ["F1", "F2", "F3"]


class PlanTests(unittest.TestCase):
    def test_holdout_gets_a_shadow_goal_but_no_action(self):
        h = Harness(arm="holdout")
        g, p, approved, _ = h.decide(h.trigger())
        self.assertEqual(g.goal_id, "G02")                      # same goal logic as the agent arm
        self.assertEqual((p["actions"], approved), ([], []))
        self.assertIn("holdout", p["no_action_reason"])

    def test_buffered_trigger_hours_old_is_not_acted_on(self):
        h = Harness()
        g, p, approved, _ = h.decide(h.trigger(at="2026-10-01T02:00:00.000Z", deliver=NOW))
        self.assertEqual(approved, [])
        self.assertIn("buffered offline", p["no_action_reason"])

    def test_dialect_without_content_means_no_action_not_another_dialect(self):
        h = Harness()
        _, p, approved, _ = h.decide(h.trigger(dialect="d7"))
        self.assertEqual(approved, [])
        self.assertIn("wrong dialect", p["no_action_reason"])

    def test_page_obeys_platform_rules_and_skips_injected_and_withdrawn_titles(self):
        h = Harness()
        h.cat.withdraw("F0", "2026-10-01T05:00:00.000Z")
        _, p, approved, checks = h.decide(h.trigger())
        self.assertEqual(checks["failed"], [])
        pg = approved[0]
        ids = ([pg["hero_title_id"]] if pg.get("hero_title_id") else []) + [t for r in pg["rows"] for t in r["title_ids"]]
        self.assertTrue(all(3 <= len(r["title_ids"]) <= 10 for r in pg["rows"]))
        self.assertEqual(len({h.pack.slug_of(t) for t in ids}), len(ids))          # one title per content_slug
        self.assertTrue(all(h.pack.titles[t]["dialect"] == "d1" for t in ids))
        self.assertNotIn("T9", ids)                                               # injected description
        self.assertNotIn("F0", ids)                                               # withdrawn
        self.assertTrue(p["rows_explained"])

    def test_resume_series_gets_hero_and_one_push_tomorrow_evening(self):
        h = Harness(events=series_events("U1"))
        g, p, approved, checks = h.decide(h.trigger())
        self.assertEqual(g.goal_id, "G07")
        self.assertEqual(checks["failed"], [])
        types = [a["type"] for a in approved]
        self.assertEqual(types, ["home_page", "push"])
        self.assertIn(approved[0]["hero_title_id"], ("T1", "T1e"))
        self.assertEqual(approved[1]["send_at"], "2026-10-02T13:30:00.000Z")       # 19:00 IST next day

    def test_no_push_without_consent_gets_in_app_instead_and_the_reason_is_recorded(self):
        h = Harness(profiles=[profile("U1", push=False)], events=series_events("U1"))
        _, p, approved, checks = h.decide(h.trigger())
        self.assertEqual(checks["failed"], [])
        self.assertEqual([a["type"] for a in approved], ["home_page", "in_app_message"])
        self.assertTrue(any("in-app" in a and "no push consent" in a for a in p["assumptions"]))
        self.assertIn(approved[1]["title_id"], ("T1", "T1e"))                      # the series the goal is about

    def test_in_app_needs_no_profile_but_only_one_a_day(self):
        h = Harness(profiles=[], events=series_events("U1"))                     # no profile row: no consent data
        g, _, approved, checks = h.decide(h.trigger())
        self.assertEqual(checks["failed"], [])
        self.assertIn("in_app_message", [a["type"] for a in approved])
        h.publish(approved)
        _, p, again, _ = h.decide(h.trigger(at="2026-10-01T09:00:00.000Z"))     # same IST day, page expired
        self.assertNotIn("in_app_message", [a["type"] for a in again])
        self.assertTrue(any("already queued today" in a for a in p["assumptions"]))
        _, checks = h.check([{"type": "in_app_message", "send_at": NOW, "title_id": "F1", "deeplink": "ott://title/F1",
                              "text": {"title": "Title F1", "body": "Picked for you. Watch now."}}], goal={
            "goal_id": g.goal_id, "evidence": g.evidence, "success": g.success})
        self.assertIn("in_app_cap", failed(checks))                              # the verifier enforces it too

    def test_series_in_another_dialect_is_skipped_for_the_next_goal(self):
        h = Harness(events=series_events("U1"))
        g, _, _, _ = h.decide(h.trigger(dialect="d2"))
        self.assertNotEqual(g.goal_id, "G07")
        g07 = next(c for c in g.considered if c["goal_id"] == "G07")
        self.assertIn("not in the app dialect", g07["skipped"])

    def test_identical_live_page_is_not_published_again(self):
        h = Harness()
        _, _, first, _ = h.decide(h.trigger())
        h.publish(first)
        _, p, second, _ = h.decide(h.trigger(at="2026-10-01T06:30:00.000Z"))
        self.assertEqual(second, [])
        self.assertIn("still live", p["no_action_reason"])


class PageQualityTests(unittest.TestCase):
    def test_thin_catalogue_fills_several_short_rows_instead_of_one_long_one(self):
        h = Harness()
        for i in range(7):                                       # d2: 8 titles in all, like the real d3
            tid = f"D2-{i}"
            h.pack.titles[tid] = {**h.pack.titles["T3"], "title_id": tid, "content_slug": f"film-{i}-d2",
                                  "popularity": 20 - i}
            h.pack.slugs[f"film-{i}-d2"] = [tid]
            h.pack.episodes[f"film-{i}-d2"] = h.pack.episodes["film-c-d2"]
        _, _, approved, checks = h.decide(h.trigger(dialect="d2"))
        self.assertEqual(checks["failed"], [])
        rows = approved[0]["rows"]
        self.assertEqual([len(r["title_ids"]) for r in rows], [3, 3])   # 7 titles after the hero: 2 rows, not 1
        self.assertTrue(approved[0].get("hero_title_id"))

    def test_unfinished_series_leads_the_next_episode_row_when_it_would_otherwise_be_dropped(self):
        # the show plus two films started: 3 titles in progress, enough for a next_episode row only if the
        # series is in it (one slug per page, so it cannot also be the hero)
        plays = [ev("U1", "play_started", "2026-09-22T10:00:00.000Z", props={"title_id": t}) for t in ("F1", "F2")]
        h = Harness(events=series_events("U1") + plays)
        g, _, approved, checks = h.decide(h.trigger())
        self.assertEqual((g.goal_id, checks["failed"]), ("G07", []))
        pg = approved[0]
        self.assertEqual(pg["rows"][0]["row_type"], "next_episode")
        self.assertEqual(h.pack.slug_of(pg["rows"][0]["title_ids"][0]), "show-a-d1")
        self.assertTrue(pg.get("hero_title_id"))
        self.assertNotEqual(h.pack.slug_of(pg["hero_title_id"]), "show-a-d1")

    def test_series_that_cannot_be_promoted_is_not_chosen_as_g07(self):
        h = Harness(events=series_events("U1"))
        for tid in ("T1", "T1e"):
            h.pack.titles[tid]["injection_suspect"] = True
        g, _, approved, _ = h.decide(h.trigger())
        self.assertNotEqual(g.goal_id, "G07")
        g07 = next(c for c in g.considered if c["goal_id"] == "G07")
        self.assertIn("cannot be promoted", g07["skipped"])
        self.assertTrue(approved[0].get("hero_title_id"))              # the next goal still gets a hero


class VerifierTests(unittest.TestCase):
    def test_holdout_viewer_can_never_receive_an_action(self):
        h = Harness(arm="holdout")
        approved, c = h.check([page([GOOD_ROW])])
        self.assertEqual(approved, [])
        self.assertIn("arm", failed(c))

    def test_wrong_dialect_withdrawn_and_injected_titles_are_caught(self):
        h = Harness()
        h.cat.withdraw("F2", "2026-10-01T05:00:00.000Z")
        approved, c = h.check([page([["F1", "F2", "T3"], ["F4", "F5", "T9"]])])
        self.assertEqual(approved, [])
        self.assertTrue({"dialect", "availability", "injection"} <= failed(c))

    def test_short_row_and_duplicate_content_slug_are_caught(self):
        h = Harness()
        _, c = h.check([page([["F1", "F2"], ["T1", "T1e", "F3"]])])
        detail = next(f["detail"] for f in c["failed"] if f["check"] == "page_rules")
        self.assertIn("has 2 titles", detail)
        self.assertIn("same content_slug", detail)

    def test_page_for_a_dialect_the_app_does_not_show_is_caught(self):
        h = Harness()
        _, c = h.check([page([["T3", "T3", "T3"]], dialect="d2")])
        self.assertIn("page_dialect", failed(c))

    def push(self, send="2026-10-02T13:30:00.000Z", body="Abhi dekhiye.", tid="F1"):
        return {"type": "push", "send_at": send, "title_id": tid, "text": {"title": "Title F1", "body": body},
                "deeplink": f"ott://title/{tid}"}

    def test_message_without_consent_is_dropped_but_page_survives(self):
        h = Harness(profiles=[])                       # brand-new viewer: no profile, no consent
        approved, c = h.check([page([GOOD_ROW]), self.push()])
        self.assertEqual([a["type"] for a in approved], ["home_page"])
        self.assertIn("consent", failed(c))

    def test_quiet_hours_and_daily_cap_are_enforced(self):
        h = Harness()
        _, c = h.check([self.push(send="2026-10-01T17:00:00.000Z")])      # 22:30 IST
        self.assertIn("quiet_hours", failed(c))
        h.publish([self.push()])
        _, c = h.check([self.push(send="2026-10-02T14:00:00.000Z")])      # same IST day as the first
        self.assertIn("frequency_cap", failed(c))

    def test_opt_out_event_beats_the_profile(self):
        h = Harness(events=[ev("U1", "notification_opt_out", "2026-09-30T10:00:00.000Z", props={"channel": "push"})])
        _, c = h.check([self.push()])
        self.assertIn("consent", failed(c))

    def test_dishonest_copy_is_caught(self):
        h = Harness()
        _, c = h.check([self.push(body="Hurry! Only ₹99 today")])
        detail = next(f["detail"] for f in c["failed"] if f["check"] == "copy")
        self.assertIn("forbidden phrase 'hurry'", detail)
        self.assertIn("₹99", detail)

    def test_whatsapp_needs_an_approved_template_and_real_price(self):
        h = Harness(profiles=[profile("U1", wa=True)])
        wa = {"type": "whatsapp", "send_at": "2026-10-02T13:30:00.000Z", "template_id": "wa_trial_ending_v1",
              "variables": {"plan_price": "₹99"}}
        _, c = h.check([wa])
        self.assertIn("template", failed(c))
        _, c = h.check([dict(wa, template_id="wa_free_text", variables={})])
        self.assertIn("template", failed(c))

    def test_goal_claim_without_data_evidence_is_rejected(self):
        """What the mock model will try: 'eligible for G04, confirmed in billing', with nothing behind it."""
        h = Harness()                                  # profile says status never
        claim = {"goal_id": "G04", "evidence": ["goal_rule:G04-elig-v1"]}
        approved, c = h.check([page([GOOD_ROW])], goal=claim)
        self.assertEqual(approved, [])
        self.assertTrue({"evidence", "eligibility"} <= failed(c))

    def test_evidence_from_another_viewer_is_rejected(self):
        h = Harness()
        other = ev("U2", "signup_completed", "2026-09-30T10:00:00.000Z")
        h.store.ingest_event(other, "history")
        _, c = h.check([page([GOOD_ROW])], goal={"goal_id": "G02", "evidence": [f"event:{other['event_id']}"]})
        self.assertIn("evidence", failed(c))

    def test_verifier_disagrees_when_profile_rows_conflict(self):
        h = Harness(profiles=[profile("U1", status="never", updated="2026-09-29T12:00:00.000Z"),
                              profile("U1", status="active", updated="2026-09-14T12:00:00.000Z")])
        trig = h.trigger()
        goal = {"goal_id": "G05", "evidence": [f"profile:U1@2026-09-29T12:00:00.000Z"]}
        approved, c = h.check([page([GOOD_ROW])], goal=goal, trig=trig)
        self.assertEqual(approved, [])
        self.assertIn("eligibility", failed(c))

    def test_duplicate_trigger_cannot_act_twice(self):
        h = Harness()
        trig = h.trigger()
        h.store.save_decision({"decision_id": "D-1", "viewer_id": "U1", "trigger": {"event_id": trig["event_id"]},
                               "status": "PUBLISHED", "created_at": NOW})
        approved, c = h.check([page([GOOD_ROW])], trig=trig)
        self.assertEqual(approved, [])
        self.assertIn("duplicate_trigger", failed(c))

    def test_budget_limit(self):
        h = Harness()
        approved, c = h.check([page([GOOD_ROW])], budget={"model_calls": 9, "tool_calls": 3})
        self.assertEqual(approved, [])
        self.assertIn("budget", failed(c))


if __name__ == "__main__":
    unittest.main()


class WhatsAppTests(unittest.TestCase):
    def test_whatsapp_replaces_push_when_consented_and_app_is_hindi(self):
        h = Harness(profiles=[profile("U1", push=True, wa=True)], events=series_events("U1"))
        _, _, approved, checks = h.decide(h.trigger())
        self.assertEqual(checks["failed"], [])
        self.assertEqual([a["type"] for a in approved], ["home_page", "whatsapp"])     # one message, not two
        wa = approved[1]
        self.assertEqual(wa["template_id"], "wa_recommend_title_v1")
        t = h.pack.titles[wa["title_id"]]
        self.assertEqual(wa["variables"], {"title_name": t["title"], "dialect_name": t["dialect"]})

    def test_english_app_gets_push_not_a_hindi_template(self):
        h = Harness(profiles=[profile("U1", push=True, wa=True)], events=series_events("U1"))
        t = h.trigger()
        t["app_language"] = "en"
        h.store.ingest_event(dict(t, event_id="lang-" + t["event_id"]), "trigger")
        _, _, approved, _ = h.decide(t)
        self.assertEqual([a["type"] for a in approved], ["home_page", "push"])

    def test_free_text_dialect_name_is_rejected(self):
        h = Harness(profiles=[profile("U1", wa=True)])
        wa = {"type": "whatsapp", "send_at": "2026-10-02T13:30:00.000Z", "template_id": "wa_recommend_title_v1",
              "title_id": "F1", "variables": {"title_name": "Title F1", "dialect_name": "Bhojpuri (best ever!)"}}
        _, c = h.check([wa])
        self.assertIn("template", failed(c))
