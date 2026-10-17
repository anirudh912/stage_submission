"""Build the durable store from the prepared pack (history up to the replay day)."""
import csv
import json
import os

from .store import Store


def build_store(db_path, pack_dir, prepared_dir, fresh=True):
    if fresh and os.path.exists(db_path):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db_path + suffix):
                os.remove(db_path + suffix)
    store = Store(db_path)
    with open(os.path.join(prepared_dir, "events_clean.jsonl")) as f:
        store.bulk_ingest((json.loads(line) for line in f), source="history")
    with open(os.path.join(prepared_dir, "profiles_clean.json")) as f:
        store.load_profiles(json.load(f))
    with open(os.path.join(pack_dir, "replay", "arms.csv")) as f:
        store.set_arms({r["user_id"]: r["arm"] for r in csv.DictReader(f)})
    return store
