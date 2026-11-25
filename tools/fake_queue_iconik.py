"""Testhulp: een nagemaakte Iconik voor de wachtrij (iconik_queue.py), met metadata-velden, views, zoeken en de
zoekopdracht van automatisch transcriberen. Gebruikt door tests/test_worker.py en tests/test_two_iconiks.py.
"""
import re
from datetime import datetime, timezone

import iconik_queue as worker


class Fake:
    """Doet alsof het Iconik is, voor de calls die worker.py gebruikt."""
    uses_login = True   # zoals een verbinding met de inlog van de gebruiker

    def __init__(self):
        self.fields = {}
        self.views = []
        self.values = {}      # asset -> {veld: waarde}
        self.dates = {}       # asset -> tijdstip waarop Iconik de status zette
        self.posts = []
        self.puts = []
        self.refuse_removal = False
        self.media = {}       # asset -> {"type", "storage", "minutes", "created", "transcript"} voor auto transcribe

    def get(self, path):
        if path == "metadata/v1/views/":
            return {"objects": self.views}
        if path == "metadata/v1/fields/":
            return {"objects": [{"name": n} for n in self.fields]}
        m = re.match(r"metadata/v1/fields/(.+?)/$", path)
        if m:
            return dict(self.fields[m.group(1)])
        m = re.match(r"metadata/v1/assets/(.+?)/assets/.+?/views/(.+?)/$", path)
        if m:
            if m.group(1) not in self.values:   # zoals Iconik: nog nooit iets ingevuld geeft een 404
                raise RuntimeError(f"GET {path} returned HTTP 404: {{\"errors\":[\"Values for object_type <assets> "
                                   f"and object_id <{m.group(1)}> and version_id <v> doesn't exist\"]}}")
            vals = self.values.get(m.group(1), {})
            out = {k: {"field_values": [{"value": v}]} for k, v in vals.items()}
            if worker.FIELD in out and m.group(1) in self.dates:
                out[worker.FIELD]["date_created"] = self.dates[m.group(1)]
            return {"metadata_values": out}
        raise AssertionError("onbekende GET " + path)

    def post(self, path, body):
        self.posts.append((path, body))
        if path == "metadata/v1/fields/":
            self.fields[body["name"]] = body
            return body
        if path == "metadata/v1/views/":
            view = dict(body, id="view-1")
            self.views.append(view)
            return view
        if path.startswith("search/v1/search/") and body.get("query"):
            return self.search_candidates(path, body)
        if path.startswith("search/v1/search/"):
            term = body["filter"]["terms"][0]
            assert term["name"] == "metadata." + worker.FIELD
            ids = [a for a, v in self.values.items() if v.get(worker.FIELD) == term["value"]]
            return {"objects": [{"id": i} for i in ids]}
        raise AssertionError("onbekende POST " + path)

    def search_candidates(self, path, body):
        """De zoekopdracht van auto transcribe, zoals die tegen het echte Iconik is getest."""
        query = body["query"]
        assert query.startswith(f"NOT _exists_:metadata.{worker.FIELD} AND NOT _exists_:transcription_text ")
        assert query.endswith(" "), "een advanced query moet op een spatie eindigen"
        assert "storage" not in query, "opslag in de query draait de hele query om"
        top = int(re.search(r"duration_milliseconds:\[1 TO (\d+)\]", query).group(1))
        terms = {term["name"]: term["value"] for term in body["filter"]["terms"]}
        assert body["sort"] == [{"name": "date_created", "order": "desc"}]
        per_page = int(re.search(r"per_page=(\d+)", path).group(1))
        hits = [a for a, m in self.media.items()
                if not self.values.get(a, {}).get(worker.FIELD) and not m["transcript"]
                and m["type"] == terms["media_type"] and m["storage"] == terms["files.storage_id"]
                and 0 < m["minutes"] * 60000 <= top]
        hits.sort(key=lambda a: self.media[a]["created"], reverse=True)
        return {"objects": [{"id": a, "date_created": self.media[a]["created"]} for a in hits[:per_page]]}

    def _call(self, method, path, json=None):
        self.puts.append((method, path, json))
        m = re.match(r"metadata/v1/fields/(.+?)/$", path)
        if m:
            if self.refuse_removal:
                old = {o["value"] for o in self.fields[m.group(1)].get("options") or []}
                if not old <= {o["value"] for o in json.get("options") or []}:
                    raise RuntimeError("HTTP 400: option in use")
            self.fields[m.group(1)].update(json)
            return {}
        if path.startswith("metadata/v1/views/"):
            for v in self.views:
                if path.endswith(v["id"] + "/"):
                    v.update(json)
            return {}
        m = re.match(r"metadata/v1/assets/(.+?)/assets/.+?/views/", path)
        assert method == "PUT" and m, path
        asset = self.values.setdefault(m.group(1), {})
        for name, item in json["metadata_values"].items():
            assert item["mode"] == "overwrite"
            if item["field_values"]:
                asset[name] = item["field_values"][0]["value"]
            else:
                asset.pop(name, None)   # leegmaken
        self.dates[m.group(1)] = datetime.now(timezone.utc).isoformat()
        return {}
