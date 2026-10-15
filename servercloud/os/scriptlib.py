"""scriptlib — Script Library backend for TruckOS / ServerCloud.

Scripts are plain text (shell/python/ollama one-liners) stored under
<data_dir>/truck/var/lib/scriptlib/<slug>.json with tags, description and
usage counter.  This is the brain of menu entry [3] in the main app.
"""

import json
import os
import re
import time

_SLAG = re.compile(r"[^a-z0-9]+")


def _slug(name):
    return _SLAG.sub("-", name.lower()).strip("-")[:64] or "untitled"


class ScriptLibrary:
    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, "truck", "var", "lib", "scriptlib")
        os.makedirs(self.dir, exist_ok=True)

    # ---------- CRUD ----------
    def save(self, name, body, kind="shell", tags=None, desc=""):
        slug = _slug(name)
        rec = {"name": name, "slug": slug, "kind": kind,
               "tags": sorted(set(t.lower() for t in (tags or []))),
               "desc": desc, "body": body,
               "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
               "uses": 0}
        old = self.get(slug)
        if old:
            rec["uses"] = old.get("uses", 0)
            rec["created"] = old.get("created", rec["updated"])
        else:
            rec["created"] = rec["updated"]
        with open(os.path.join(self.dir, slug + ".json"), "w",
                  encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
        return rec

    def get(self, slug):
        p = os.path.join(self.dir, _slug(slug) + ".json")
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def delete(self, slug):
        p = os.path.join(self.dir, _slug(slug) + ".json")
        if os.path.exists(p):
            os.remove(p)
            return True
        return False

    def list(self, tag=None, query=None):
        out = []
        try:
            files = sorted(os.listdir(self.dir))
        except OSError:
            return out
        for fn in files:
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.dir, fn), encoding="utf-8") as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if tag and tag.lower() not in rec.get("tags", []):
                continue
            if query:
                hay = (rec.get("name", "") + " " + rec.get("desc", "") + " "
                       + " ".join(rec.get("tags", []))).lower()
                if query.lower() not in hay:
                    continue
            out.append(rec)
        return out

    def mark_used(self, slug):
        rec = self.get(slug)
        if rec:
            rec["uses"] = rec.get("uses", 0) + 1
            with open(os.path.join(self.dir, _slug(slug) + ".json"), "w",
                      encoding="utf-8") as f:
                json.dump(rec, f, indent=2)

    # ---------- seed catalog (server ops helpers) ----------
    def seed_defaults(self):
        if self.list():
            return 0
        defaults = [
            ("disk-usage-report", "du -h --max-depth=1 / | sort -rh | head -20",
             "shell", ["disk", "reporting"], "Top-level disk hogs"),
            ("check-ollama-health", "curl -s http://localhost:11434/api/tags | head -c 400",
             "shell", ["ollama", "health"], "Probe local Ollama API"),
            ("tail-server-log", "journalctl -u servercloud -n 50 --no-pager 2>/dev/null || tail -n 50 ~/.servercloud/truck/var/log/init.log",
             "shell", ["logs", "debug"], "Last log lines from init/services"),
            ("gpu-proc-scan", "ps aux --sort=-%mem | head -15",
             "shell", ["processes", "monitoring"], "Memory-hungriest processes"),
            ("model-size-audit", "du -sh ~/.ollama/models/* 2>/dev/null | sort -rh",
             "shell", ["ollama", "disk"], "Size of each downloaded model"),
            ("lan-hosts-scan", "ping -c1 -W1 10.0.0.1 >/dev/null 2>&1; ip neigh 2>/dev/null || arp -a",
             "shell", ["network", "scan"], "List reachable LAN hosts (ARP table)"),
        ]
        for name, body, kind, tags, desc in defaults:
            self.save(name, body, kind, tags, desc)
        return len(defaults)
