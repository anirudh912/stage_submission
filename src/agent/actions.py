"""Action builder: turns one goal into a proportionate plan (home page, at most one message) or into NO action.

Design rules:
  * The viewer is in the app right now, so the main lever is the home page for the dialect the app shows.
  * A message is only planned when the goal needs the viewer to come back later (G03 / G07 / G06), or for an
    honest trial-ending reminder (G04, only when the trial end date is known). It is scheduled for the next
    allowed evening slot, never during quiet hours, and never twice on one IST day.
  * Doing nothing is a normal outcome with a recorded reason (stale trigger, unknown dialect, too little content,
    page already live, no goal).
  * Titles are picked by content_slug (one per page), in the page dialect, available at publish time, and never
    from a title flagged for injected instructions. Descriptions are never used for anything.
"""
from collections import Counter
from datetime import timedelta

from .goals import NEW_RELEASE_DAYS, _released_within
from .timeutil import IST, ist_day, iso, ts

STALE_TRIGGER_S = 600            # older than this, the viewer is not in the app any more (buffered offline)
PAGE_TTL = timedelta(minutes=120)
MAX_ROWS, MIN_ROW, MAX_ROW = 6, 3, 10
MESSAGE_HOUR_IST = 19            # next allowed evening slot (quiet hours are 22:00-08:00 IST)
MESSAGE_GOALS = {"G03", "G06", "G07"}
PUSH_TITLE_MAX, PUSH_BODY_MAX = 40, 120


class Catalog:
    """Catalogue view with availability that respects live withdrawals (control events)."""

    def __init__(self, pack):
        self.pack = pack
        self.titles = pack.titles
        self.withdrawn = {}          # title_id -> withdrawn_at (iso)

    def withdraw(self, title_id, at):
        self.withdrawn[title_id] = at

    def available(self, tid, at):
        t = self.titles.get(tid)
        if not t:
            return False
        at = iso(ts(at))
        until = t["available_until"]
        w = self.withdrawn.get(tid)
        if w and (until is None or w < until):
            until = w
        return (t["available_from"] is None or t["available_from"] <= at) and (until is None or at < until)

    def promotable(self, tid, dialect, at):
        t = self.titles.get(tid)
        return bool(t) and t["dialect"] == dialect and not t["injection_suspect"] and self.available(tid, at)

    def in_dialect(self, dialect, at):
        return [t for t in self.titles.values() if self.promotable(t["title_id"], dialect, at)]


# ------------------------------------------------------------------ helpers

def _pick_variant(catalog, slug, dialect, language, at):
    """One title_id per content_slug: the language variant the app uses, else any valid variant."""
    ids = [tid for tid in catalog.pack.slugs.get(slug, []) if catalog.promotable(tid, dialect, at)]
    if not ids:
        return None
    ids.sort(key=lambda tid: (catalog.titles[tid]["language"] != language, tid))
    return ids[0]


def _watch_profile(view, pack):
    """Genres the viewer plays, slugs they finished, and slugs in progress (from cited events)."""
    genres, done, started = Counter(), set(), {}
    for e in view.derived["plays"]:
        t = pack.title(e["properties"].get("title_id"))
        if t:
            genres.update(t["genres"])
            started.setdefault(t["content_slug"], e)
    for e in view.derived["completions"]:
        t = pack.title(e["properties"].get("title_id"))
        if t and t["content_type"] == "film":
            done.add(t["content_slug"])
    return genres, done, started


def _ranked(candidates, prefer_free):
    # popularity first; for viewers who cannot play premium titles, free titles go first
    return sorted(candidates, key=lambda t: ((t["is_premium"] if prefer_free else False), -t["popularity"],
                                             t["title_id"]))


class _Page:
    def __init__(self, catalog, dialect, language, at):
        self.catalog, self.dialect, self.language, self.at = catalog, dialect, language, at
        self.used, self.rows, self.hero = set(), [], None

    def take(self, slugs, limit=MAX_ROW):
        out = []
        for slug in slugs:
            if slug in self.used:
                continue
            tid = _pick_variant(self.catalog, slug, self.dialect, self.language, self.at)
            if tid:
                out.append((slug, tid))
            if len(out) == limit:
                break
        return out

    def add_row(self, row_type, slugs, why):
        if len(self.rows) >= MAX_ROWS:
            return False
        picked = self.take(slugs)
        if len(picked) < MIN_ROW:
            return False
        self.used.update(s for s, _ in picked)
        self.rows.append({"row_type": row_type, "title_ids": [tid for _, tid in picked], "why": why})
        return True

    def set_hero(self, slug, why):
        picked = self.take([slug], 1)
        if picked:
            self.used.add(picked[0][0])
            self.hero = {"title_id": picked[0][1], "why": why}


# ------------------------------------------------------------------ plan

def actionable(goal, view, catalog, at):
    """Can this goal be served in the dialect the app shows? (Arm-independent, so shadow goals match.)"""
    d = view.value("dialect")
    if goal.goal_id == "G07":
        slug = (goal.success.get("target") or {}).get("content_slug", "")
        if d is None or not slug.endswith("-" + d):
            return False, f"the unfinished series {slug} is not in the app dialect {d}"
        if not any(catalog.available(t, at) for t in catalog.pack.slugs.get(slug, [])):
            return False, f"the unfinished series {slug} is not available now"
    return True, None


def choose_actionable_goal(view, registry, pack, catalog, at, open_goal=None):
    """choose_goal, skipping goals that cannot be served for this viewer right now."""
    from .goals import choose_goal
    skipped = []
    while True:
        g = choose_goal(view, registry, pack, open_goal=open_goal, exclude=[s["goal_id"] for s in skipped])
        if g.goal_id is None or g.continuing:
            break
        ok, why = actionable(g, view, catalog, at)
        if ok:
            break
        skipped.append({"goal_id": g.goal_id, "why_skipped": why})
    for s in skipped:
        for c in g.considered:
            if c["goal_id"] == s["goal_id"]:
                c["skipped"] = s["why_skipped"]
    return g


def plan(view, goal, pack, catalog, store, publish_at, arm):
    """Return a plan dict. `goal` is a goals.GoalChoice. Never raises for normal "do nothing" cases."""
    at = iso(ts(publish_at))
    p = {"actions": [], "no_action_reason": None, "content": [], "assumptions": [], "evidence": [],
         "rows_explained": [], "confidence": goal.confidence, "confidence_reason": goal.confidence_reason}

    def nothing(reason):
        p["no_action_reason"] = reason
        p["confidence_reason"] = reason
        return p

    if goal.goal_id is None:
        return nothing(f"no goal: {goal.reason}")
    if arm != "agent":
        return nothing(f"viewer is in the '{arm}' arm: shadow decision only, no action of any kind")
    age = view.value("trigger_age_s") if "trigger_age_s" in view.facts else 0
    if age is not None and age > STALE_TRIGGER_S:
        return nothing(f"trigger is {age / 3600:.1f} h old (buffered offline); the viewer is not in the app now")
    dialect = view.value("dialect")
    if dialect is None:
        f = view.f("dialect")
        return nothing(f"app dialect {view.trigger.get('app_dialect') if view.trigger else None!r} has no catalogue "
                       f"content; showing another dialect would be the wrong dialect ({f.rule})")

    page = build_page(view, goal, pack, catalog, dialect, at)
    live = live_page(store, view.user_id, at)
    if live and live["dialect"] == dialect and live.get("goal_id") == goal.goal_id and \
            _same_content(live, page):
        page = None
        p["assumptions"].append(f"page {live['action_id']} for {dialect} is still live and unchanged")
    if page:
        p["actions"].append(page)
        p["rows_explained"] = [{"row_type": r["row_type"], "why": r.pop("why")} for r in page["rows"]]
        if page.get("_hero_why"):
            p["rows_explained"].insert(0, {"row_type": "hero", "why": page.pop("_hero_why")})
        p["content"] = [f"title:{t}" for t in ([page["hero_title_id"]] if page.get("hero_title_id") else []) +
                        [t for r in page["rows"] for t in r["title_ids"]]]
        p["evidence"] += [f"catalogue:{t.split(':')[1]}" for t in p["content"][:3]]

    msg, why_not = build_message(view, goal, pack, catalog, store, dialect, at)
    if msg:
        p["actions"].append(msg)
    elif why_not:
        p["assumptions"].append(f"no message: {why_not}")

    if not p["actions"]:
        if live:
            return nothing(f"personalised page {live['action_id']} is still live for this goal; nothing new to add")
        return nothing(f"fewer than {MIN_ROW} valid titles in {dialect} for any row, and no message applies")
    p["assumptions"].append("viewer reaches the home page within seconds of this app open")
    return p


def build_page(view, goal, pack, catalog, dialect, at):
    lang = view.value("language")
    genres, done, started = _watch_profile(view, pack)
    st = view.value("subscription_status")
    prefer_free = st not in ("active", "trial_active") and goal.goal_id not in ("G04", "G05", "G06")
    pool = [t for t in catalog.in_dialect(dialect, at) if t["content_slug"] not in done]
    ranked = _ranked(pool, prefer_free)
    slugs = lambda ts_: list(dict.fromkeys(t["content_slug"] for t in ts_))
    pg = _Page(catalog, dialect, lang, at)

    # hero: the one thing this goal wants the viewer to do next
    series = [s for s in view.value("unfinished_series") if s["content_slug"].endswith("-" + dialect)]
    if goal.goal_id == "G07" and goal.success and goal.success.get("target"):
        pg.set_hero(goal.success["target"]["content_slug"], "the unfinished series this goal is about")
    elif goal.goal_id in ("G01", "G02"):
        short = [t for t in ranked if t["content_type"] == "micro_drama"] or ranked
        if short:
            pg.set_hero(short[0]["content_slug"], "short, popular, free to watch: easiest first episode to finish")
    elif goal.goal_id in ("G04", "G05", "G06"):
        prem = [t for t in ranked if t["is_premium"]]
        if prem:
            pg.set_hero(prem[0]["content_slug"], "most-played premium title: the reason to pay")
    elif goal.goal_id == "G08":
        new = [t for t in ranked if _released_within(t, at, NEW_RELEASE_DAYS)]
        if new:
            pg.set_hero(new[0]["content_slug"], f"released in the last {NEW_RELEASE_DAYS} days")
    elif ranked:
        pg.set_hero(ranked[0]["content_slug"], "most-played title in this dialect")

    in_progress = [s["content_slug"] for s in series] + \
                  [s for s in started if s.endswith("-" + dialect) and s not in done]
    pg.add_row("next_episode", list(dict.fromkeys(in_progress)), "series the viewer has started in this dialect")
    if goal.goal_id == "G08":
        pg.add_row("new_releases", slugs(t for t in ranked if _released_within(t, at, NEW_RELEASE_DAYS)),
                   f"released in the last {NEW_RELEASE_DAYS} days (the goal)")
    if genres:
        top = [g for g, _ in genres.most_common(2)]
        pg.add_row("because_you_watched", slugs(t for t in ranked if set(t["genres"]) & set(top)),
                   f"shares a genre with what the viewer plays most ({', '.join(top)})")
        pg.add_row("genre", slugs(t for t in ranked if top[0] in t["genres"]), f"top genre: {top[0]}")
    if goal.goal_id in ("G01", "G02"):
        pg.add_row("popular_in_dialect", slugs(t for t in ranked if t["content_type"] == "micro_drama"),
                   "short episodes are the easiest first watch")
    if goal.goal_id in ("G04", "G05", "G06"):
        pg.add_row("popular_in_dialect", slugs(t for t in ranked if t["is_premium"]),
                   "premium titles: what a plan unlocks")
    pg.add_row("popular_in_dialect", slugs(ranked), "most-played in this dialect")
    if goal.goal_id != "G08":
        pg.add_row("new_releases", slugs(t for t in ranked if _released_within(t, at, NEW_RELEASE_DAYS)),
                   f"released in the last {NEW_RELEASE_DAYS} days")
    for g in sorted({g for t in ranked for g in t["genres"]}):
        if len(pg.rows) >= 5:
            break
        pg.add_row("genre", slugs(t for t in ranked if g in t["genres"]), f"genre: {g}")

    if not pg.rows and not pg.hero:
        return None
    page = {"type": "home_page", "dialect": dialect, "rows": pg.rows, "goal_id": goal.goal_id}
    if pg.hero:
        page["hero_title_id"] = pg.hero["title_id"]
        page["_hero_why"] = pg.hero["why"]
    return page


def _same_content(live, page):
    if page is None:
        return True
    ids = lambda p: (p.get("hero_title_id"), [tuple(r["title_ids"]) for r in p["rows"]])
    return ids(live) == ids(page)


def live_page(store, user_id, at):
    """Our most recent home page for this viewer that is still inside its TTL at `at`."""
    pages = [a for a in store.actions_for(user_id) if a["type"] == "home_page" and a.get("published_at")
             and ts(a["published_at"]) <= ts(at) < ts(a["published_at"]) + PAGE_TTL]
    return pages[-1] if pages else None


def next_send_slot(at):
    """19:00 IST on the next IST day: the viewer is in the app now, so a nudge only helps a later visit
    (and G03 needs a later day by definition). 19:00 is outside quiet hours (22:00-08:00 IST)."""
    local = ts(at).astimezone(IST) + timedelta(days=1)
    return iso(local.replace(hour=MESSAGE_HOUR_IST, minute=0, second=0, microsecond=0))


def messages_on_day(store, user_id, day, until):
    """Push/WhatsApp already sent or scheduled for one IST day (ours + delivered events in the stream)."""
    n = [a for a in store.actions_for(user_id) if a["type"] in ("push", "whatsapp")
         and ist_day(a.get("send_at") or a["published_at"]) == day]
    d = [e for e in store.events_for(user_id, until=until) if e["event"] == "message_delivered"
         and e["properties"].get("channel") in ("push", "whatsapp") and ist_day(e["received_at"]) == day]
    return len(n) + len(d)


def build_message(view, goal, pack, catalog, store, dialect, at):
    """At most one message. Returns (message|None, reason_if_none)."""
    gid = goal.goal_id
    send_at = next_send_slot(at)
    if goal.deadline and send_at >= goal.deadline:
        return None, "next allowed send slot is after the goal deadline"
    if gid == "G04":
        end = view.value("trial_ends_at") if "trial_ends_at" in view.facts else None
        if not end or not (ts(at) <= ts(end) <= ts(at) + timedelta(days=3)):
            return None, "trial end date unknown or not soon: 'trial ending' copy would not be honest"
        if not view.value("consent_whatsapp"):
            return None, "no WhatsApp consent"
        if view.value("language") != "hin":
            return None, "approved WhatsApp templates are Hindi only; app language is not Hindi"
        if messages_on_day(store, view.user_id, ist_day(send_at), at):
            return None, "a message is already planned or delivered that IST day"
        price = next(pl for pl in pack.pricing["plans"] if pl["plan_id"] == "P_MONTH")
        return {"type": "whatsapp", "send_at": send_at, "template_id": "wa_trial_ending_v1",
                "variables": {"plan_price": f"₹{price['price_inr']}"}, "goal_id": gid,
                "evidence": ["pricing:P_MONTH"]}, None
    if gid not in MESSAGE_GOALS:
        return None, f"goal {gid} is served by the home page while the viewer is in the app"
    if not view.value("consent_push"):
        return None, f"no push consent ({view.f('consent_push').source})"
    if messages_on_day(store, view.user_id, ist_day(send_at), at):
        return None, "a message is already planned or delivered that IST day"
    if gid == "G07" and goal.success.get("target"):
        tid = _pick_variant(catalog, goal.success["target"]["content_slug"], dialect, view.value("language"), send_at)
    else:
        cand = _ranked(catalog.in_dialect(dialect, send_at), prefer_free=gid == "G03")
        tid = cand[0]["title_id"] if cand else None
    if not tid or not catalog.available(tid, send_at):
        return None, "no title in the viewer's dialect is available at the send time"
    title = pack.titles[tid]["title"]
    hin = view.value("language") == "hin"
    if gid == "G07":
        text = {"title": _clip(title, PUSH_TITLE_MAX),
                "body": "Aapki kahani wahin ruki hai. Agla episode dekhiye." if hin
                else "Pick up where you left off: your next episode is waiting."}
    else:
        text = {"title": _clip(title, PUSH_TITLE_MAX),
                "body": "Aaj ke liye ek nayi kahani. Abhi dekhiye." if hin else "Something to watch tonight."}
    return {"type": "push", "send_at": send_at, "title_id": tid, "text": text,
            "deeplink": f"ott://title/{tid}", "goal_id": gid, "evidence": [f"catalogue:{tid}"]}, None


def _clip(s, n):
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"
