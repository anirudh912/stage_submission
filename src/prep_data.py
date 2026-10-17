#!/usr/bin/env python3
"""Clean and index the data pack (standard library only).

    python src/prep_data.py --pack ../data_pack/one_user_one_agent_pack --out prepared

Nothing is decided here: the script only removes exact duplicates, normalises timestamps,
flags problems and builds lookup indexes. Conflicts are kept visible, never silently resolved.
"""
import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
CATALOGUE_DIALECTS = {"d1", "d2", "d3", "d4", "d5", "d6"}
CLOCK_SKEW_LIMIT_S = 300          # received_at - sent_at beyond this => phone clock is wrong
LATE_LIMIT_S = 3600               # occurred -> received beyond this => buffered / late event

# Text in the data that tries to instruct an AI. Flagged, never obeyed.
INJECTION_PATTERNS = re.compile(
    r"(ignore (your|all|previous)|system note|assistant instruction|for ai agents|skip (all )?checks"
    r"|every user|pre-approved|mark every)", re.I)


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if dt else None


def read_jsonl(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)


def bool_field(v):
    return {"true": True, "false": False}.get((v or "").strip().lower())


# ---------------------------------------------------------------- events

def annotate(e):
    """Add derived timing and dialect flags to a raw event (copy)."""
    occ, sent, rec = ts(e["occurred_at"]), ts(e["sent_at"]), ts(e["received_at"])
    skew = (rec - sent).total_seconds()            # server minus phone clock (send is ~instant)
    buffered = (sent - occ).total_seconds()        # both phone clock, so skew cancels
    out = dict(e)
    out["_q"] = {
        "clock_skew_s": round(skew, 1),
        "clock_suspect": abs(skew) > CLOCK_SKEW_LIMIT_S,
        "buffered_s": round(buffered, 1),
        # best estimate of when it really happened, on the server clock
        "occurred_at_est": iso(occ + timedelta(seconds=skew)),
        "late": (rec - (occ + timedelta(seconds=skew))).total_seconds() > LATE_LIMIT_S,
        "dialect_valid": e.get("app_dialect") in CATALOGUE_DIALECTS,
    }
    return out


def dedupe(events):
    """Keep one copy per event_id (earliest received_at); count redeliveries."""
    best, copies = {}, Counter()
    for e in events:
        k = e["event_id"]
        copies[k] += 1
        if k not in best or e["received_at"] < best[k]["received_at"]:
            best[k] = e
    for k, e in best.items():
        e["_q"]["delivered_copies"] = copies[k]
    return list(best.values()), sum(c - 1 for c in copies.values())


def user_index(events):
    """Per-user facts derived from events, each with the event_id that proves it."""
    idx = defaultdict(lambda: {
        "event_count": 0, "first_received_at": None, "last_received_at": None,
        "signup": None, "first_play": None, "first_episode_completed": None,
        "purchases": [], "renewals": [], "lapses": [], "opt_outs": [],
        "dialects_seen": Counter(), "dialect_changes": [], "deeplink_dialects": [],
        "last_app_open": None,
    })
    for e in sorted(events, key=lambda x: x["received_at"]):
        u = idx[e["user_id"]]
        ref = {"event_id": e["event_id"], "received_at": e["received_at"],
               "occurred_at_est": e["_q"]["occurred_at_est"]}
        p = e.get("properties") or {}
        ev = e["event"]
        u["event_count"] += 1
        u["first_received_at"] = u["first_received_at"] or e["received_at"]
        u["last_received_at"] = e["received_at"]
        u["dialects_seen"][str(e.get("app_dialect"))] += 1
        if ev == "signup_completed" and not u["signup"]:
            u["signup"] = ref
        elif ev == "play_started" and not u["first_play"]:
            u["first_play"] = {**ref, "title_id": p.get("title_id")}
        elif ev == "episode_completed" and not u["first_episode_completed"]:
            u["first_episode_completed"] = {**ref, "title_id": p.get("title_id")}
        elif ev == "purchase_completed":
            u["purchases"].append({**ref, **p})
        elif ev == "subscription_renewed":
            u["renewals"].append({**ref, **p})
        elif ev == "subscription_lapsed":
            u["lapses"].append(ref)
        elif ev == "notification_opt_out":
            u["opt_outs"].append({**ref, "channel": p.get("channel")})
        elif ev == "app_dialect_changed":
            u["dialect_changes"].append({**ref, "from": p.get("from"), "to": p.get("to")})
        elif ev == "app_open":
            u["last_app_open"] = ref
            m = re.match(r"^ott://c/([^/]+)/([^/]+)/title/([^/]+)$", p.get("deeplink") or "")
            if m:
                u["deeplink_dialects"].append({**ref, "campaign": m[1], "dialect": m[2], "title_id": m[3]})
    for u in idx.values():
        u["dialects_seen"] = dict(u["dialects_seen"])
    return dict(idx)


# ---------------------------------------------------------------- profiles

def profiles(path):
    rows = list(csv.DictReader(open(path)))
    by_user = defaultdict(list)
    for r in rows:
        for k in ("notifications_opt_in", "whatsapp_opt_in"):
            r[k] = bool_field(r[k])
        for k, v in r.items():
            if v == "":
                r[k] = None
        by_user[r["user_id"]].append(r)
    out = {}
    for uid, versions in by_user.items():
        versions.sort(key=lambda r: r["updated_at"], reverse=True)
        chosen = versions[0]
        conflicts = sorted({k for v in versions[1:] for k in v if v[k] != chosen[k]} - {"updated_at"})
        out[uid] = {
            **chosen,
            "dialect_valid": chosen["preferred_dialect"] in CATALOGUE_DIALECTS,
            "versions": len(versions),
            # newest updated_at is only a default; events must confirm status-type fields
            "conflict_fields": conflicts,
            "other_versions": versions[1:],
        }
    return out, len(rows)


# ---------------------------------------------------------------- catalogue

def catalogue(cat_path, ep_path):
    eps = defaultdict(list)
    for r in csv.DictReader(open(ep_path)):
        eps[r["content_slug"]].append((int(r["season"]), int(r["episode"]), r["episode_id"], int(r["minutes"])))
    ep_index = {}
    for slug, lst in eps.items():
        lst.sort()
        ep_index[slug] = {"count": len(lst), "episode_ids": [x[2] for x in lst],
                          "total_minutes": sum(x[3] for x in lst)}

    titles, by_slug = {}, defaultdict(list)
    for r in csv.DictReader(open(cat_path)):
        desc = r["description"] or ""
        t = {
            "title_id": r["title_id"], "content_slug": r["content_slug"], "title": r["title"],
            "dialect": r["dialect"], "language": r["language"], "content_type": r["content_type"],
            "genres": [g for g in r["genres"].split("|") if g], "release_date": r["release_date"],
            "available_from": r["available_from"] or None, "available_until": r["available_until"] or None,
            "is_premium": bool_field(r["is_premium"]), "description": desc,
            "injection_suspect": bool(INJECTION_PATTERNS.search(desc)),
            "episode_count": ep_index.get(r["content_slug"], {}).get("count", 0),
        }
        titles[t["title_id"]] = t
        by_slug[t["content_slug"]].append(t["title_id"])
    return titles, dict(by_slug), ep_index


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    P = lambda *p: os.path.join(a.pack, *p)
    os.makedirs(a.out, exist_ok=True)

    raw = [annotate(e) for e in read_jsonl(P("data", "events.jsonl"))]
    events, dup_events = dedupe(raw)
    events.sort(key=lambda e: e["received_at"])
    write_jsonl(os.path.join(a.out, "events_clean.jsonl"), events)

    users = user_index(events)
    write_json(os.path.join(a.out, "user_event_index.json"), users)

    profs, prof_rows = profiles(P("data", "profiles_snapshot.csv"))
    write_json(os.path.join(a.out, "profiles_clean.json"), profs)

    titles, by_slug, ep_index = catalogue(P("data", "catalogue.csv"), P("data", "episodes.csv"))
    # popularity = distinct viewers who started a play, per content_slug (language variants pooled)
    players = defaultdict(set)
    for e in events:
        if e["event"] == "play_started":
            t = titles.get((e.get("properties") or {}).get("title_id"))
            if t:
                players[t["content_slug"]].add(e["user_id"])
    for t in titles.values():
        t["popularity"] = len(players.get(t["content_slug"], ()))
    write_json(os.path.join(a.out, "titles.json"), titles)
    write_json(os.path.join(a.out, "slug_index.json"), by_slug)
    write_json(os.path.join(a.out, "episode_index.json"), ep_index)

    # replay triggers: dedupe the same way, keep delivery order
    trig_raw = [annotate(t) for t in read_jsonl(P("replay", "triggers.jsonl"))]
    trig_ids = Counter(t["event_id"] for t in trig_raw)
    for t in trig_raw:
        t["_q"]["delivered_copies"] = trig_ids[t["event_id"]]
        t["_q"]["age_at_delivery_s"] = round(
            (ts(t["deliver_at"]) - ts(t["_q"]["occurred_at_est"])).total_seconds(), 1)
    trig_raw.sort(key=lambda t: t["deliver_at"])
    write_jsonl(os.path.join(a.out, "triggers_annotated.jsonl"), trig_raw)

    arms = {r["user_id"]: r["arm"] for r in csv.DictReader(open(P("replay", "arms.csv")))}
    trig_users = {t["user_id"] for t in trig_raw}
    flagged = sorted(t for t, v in titles.items() if v["injection_suspect"])
    report = {
        "events": {
            "raw_rows": len(raw), "unique": len(events), "duplicate_rows_removed": dup_events,
            "users": len(users),
            "clock_suspect": sum(e["_q"]["clock_suspect"] for e in events),
            "late_over_1h": sum(e["_q"]["late"] for e in events),
            "invalid_dialect": dict(Counter(str(e.get("app_dialect")) for e in events
                                            if not e["_q"]["dialect_valid"])),
            "by_type": dict(Counter(e["event"] for e in events).most_common()),
        },
        "profiles": {
            "rows": prof_rows, "users": len(profs),
            "users_with_duplicate_rows": sum(p["versions"] > 1 for p in profs.values()),
            "duplicate_conflict_fields": dict(Counter(f for p in profs.values() for f in p["conflict_fields"])),
            "missing_or_invalid_dialect": sum(not p["dialect_valid"] for p in profs.values()),
            "status": dict(Counter(p["subscription_status"] for p in profs.values())),
            "event_users_without_profile": len(set(users) - set(profs)),
        },
        "catalogue": {
            "title_rows": len(titles), "content_slugs": len(by_slug),
            "slugs_with_language_variants": sum(len(v) > 1 for v in by_slug.values()),
            "titles_per_dialect": dict(Counter(t["dialect"] for t in titles.values())),
            "with_available_until": sum(bool(t["available_until"]) for t in titles.values()),
            "injection_suspects": flagged,
            "titles_without_episodes": sum(t["episode_count"] == 0 for t in titles.values()),
        },
        "replay": {
            "triggers": len(trig_raw), "unique_triggers": len(trig_ids),
            "duplicate_deliveries": len(trig_raw) - len(trig_ids),
            "older_than_1d_at_delivery": sum(t["_q"]["age_at_delivery_s"] > 86400 for t in trig_raw),
            "clock_suspect": sum(t["_q"]["clock_suspect"] for t in trig_raw),
            "users": len(trig_users),
            "users_without_profile": len(trig_users - set(profs)),
            "users_without_any_history": len(trig_users - set(users)),
            "users_by_arm": dict(Counter(arms.get(u, "missing") for u in trig_users)),
        },
    }
    write_json(os.path.join(a.out, "data_quality_report.json"), report)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
