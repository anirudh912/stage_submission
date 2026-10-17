"""Read-only access to the config and the prepared catalogue (output of prep_data.py)."""
import json
import os

CATALOGUE_DIALECTS = {"d1", "d2", "d3", "d4", "d5", "d6"}


def _load(path):
    with open(path) as f:
        return json.load(f)


class Pack:
    def __init__(self, pack_dir, prepared_dir):
        self.pack_dir = pack_dir
        self.prepared_dir = prepared_dir
        cfg = lambda name: _load(os.path.join(pack_dir, "config", name))
        self.goals_config = cfg("goals.json")
        self.interventions = cfg("interventions.json")
        self.pricing = cfg("pricing.json")
        prep = lambda name: _load(os.path.join(prepared_dir, name))
        self.titles = prep("titles.json")          # title_id -> title record
        self.slugs = prep("slug_index.json")       # content_slug -> [title_id]
        self.episodes = prep("episode_index.json")  # content_slug -> {count, episode_ids, total_minutes}

    def title(self, title_id):
        return self.titles.get(title_id)

    def slug_of(self, title_id):
        t = self.titles.get(title_id)
        return t["content_slug"] if t else None

    def episode_position(self, slug, episode_id):
        """1-based position of an episode within its content, or None if unknown."""
        ids = self.episodes.get(slug, {}).get("episode_ids", [])
        return ids.index(episode_id) + 1 if episode_id in ids else None
