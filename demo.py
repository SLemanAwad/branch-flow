"""Build docs/index.html from made-up data, so the page can be shown without a real repository.

Run:  python -I demo.py
Team, branches and PRs are fictional: Dev1..Dev4, branch_1.., PR #1..
"""
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "docs" / "index.html"
NOW = datetime.now(timezone.utc).replace(minute=30, second=0, microsecond=0)
rnd = random.Random(7)

CONFIG = {
    "trunks": [
        {"branch": "main", "note": "releases", "releases": True},
        {"branch": "production", "note": "feature merges"},
        {"branch": "integration", "lane": "dev4"},
    ],
    "people": [
        {"id": "dev1", "name": "Dev1", "note": "backend"},
        {"id": "dev2", "name": "Dev2", "note": "frontend"},
        {"id": "dev3", "name": "Dev3", "note": "data"},
        {"id": "dev4", "name": "Dev4", "note": "fork PRs into integration"},
    ],
    "base": "production",
    "flows": [
        {"from": "production", "to": "main", "hint": "merge production into main", "list_branches": True},
        {"from": "main", "to": "production", "hint": "merge main into production"},
        {"from": "main", "to": "integration", "hint": "merge main into integration"},
    ],
    "window_days": 75, "stale_days": 7, "abandoned_days": 30,
}

SUBJECTS = ["add export endpoint", "fix pagination on search", "cache lookup results", "validate upload size",
            "refactor auth middleware", "update chart colours", "handle empty filters", "add audit log entry",
            "speed up report query", "fix date parsing", "add retry on timeout", "tidy settings page"]


def iso(d):
    return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def sha():
    return "%08x" % rnd.getrandbits(32)


def ago(days, hours=0):
    return NOW - timedelta(days=days, hours=hours)


branches, events = [], {"main": [], "production": [], "integration": []}
n = 0


def branch(lane, start, length=None, merged=True, waiting=False, commits=None, empty=False, idle=None, behind=0):
    global n
    n += 1
    name = f"branch_{n}"
    created = ago(start)
    c = 0 if empty else (commits or rnd.randint(2, 14))
    b = {"name": name, "lane": lane, "tip": sha(), "created": iso(created), "commits": c, "empty": empty,
         "first_commit": rnd.choice(SUBJECTS) if c else "", "last_commit": rnd.choice(SUBJECTS) if c else "",
         "pr": None, "creator": "dev" + lane[-1] + "-gh"}
    if merged:
        end = created + timedelta(days=length or rnd.uniform(1, 6))
        b.update(status="merged", last=iso(end - timedelta(hours=3)),
                 merges=[{"into": "production", "date": iso(end), "sha": sha()}],
                 **{"in": {"production": True, "main": not waiting, "integration": not waiting}})
        events["production"].append({"kind": "merge", "date": iso(end), "sha": b["merges"][0]["sha"], "src": name,
                                     "pr": None, "author": "Lead", "subject": f"Merge branch '{name}' into production",
                                     "missing": ["main"] if waiting else []})
    else:
        last = ago(idle if idle is not None else 0, 2)
        b.update(status="open", last=iso(last), merges=[], behind_base=behind, idle_days=idle or 0,
                 stale=(idle or 0) >= 7, **{"in": {"production": False, "main": False, "integration": False}})
    branches.append(b)


# Dev1: steady flow, one waiting for main, one active
for s in (70, 61, 52, 44, 33, 24, 15):
    branch("dev1", s)
branch("dev1", 4, length=2.2, waiting=True)
branch("dev1", 3, merged=False, commits=6, idle=0, behind=4)
# Dev2: one long-idle branch with work, one pushed and empty
for s in (68, 55, 41, 30, 18):
    branch("dev2", s)
branch("dev2", 26, merged=False, commits=11, idle=19, behind=63)
branch("dev2", 2, merged=False, empty=True, idle=2, behind=0)
# Dev3: bigger pieces of work
for s, length in ((72, 9), (58, 7), (45, 12), (27, 8), (12, 6)):
    branch("dev3", s, length=length)
branch("dev3", 5, merged=False, commits=9, idle=1, behind=7)

# Dev4: fork PRs into integration
fork_prs = []
for i, s in enumerate((66, 57, 47, 36, 25, 16, 8), start=1):
    created = ago(s)
    merged = created + timedelta(days=rnd.uniform(0.5, 2.5))
    fork_prs.append({"name": f"branch_{n + i}", "lane": "dev4", "number": i, "title": f"PR{i}: " + rnd.choice(SUBJECTS),
                     "base": "integration", "state": "MERGED", "draft": False, "review": "APPROVED",
                     "created": iso(created), "merged": iso(merged), "closed": iso(merged), "updated": iso(merged),
                     "url": f"https://github.com/acme/shop-api/pull/{i}", "author": "dev4-gh"})
    events["integration"].append({"kind": "merge", "date": iso(merged), "sha": sha(), "src": f"branch_{n + i}",
                                  "pr": i, "author": "Lead", "subject": f"Merge pull request #{i} from dev4-gh/branch_{n + i}",
                                  "missing": []})
created = ago(1, 5)
fork_prs.append({"name": f"branch_{n + 8}", "lane": "dev4", "number": 8, "title": "PR8: " + rnd.choice(SUBJECTS),
                 "base": "integration", "state": "OPEN", "draft": True, "review": "REVIEW_REQUIRED",
                 "created": iso(created), "merged": None, "closed": None, "updated": iso(ago(0, 6)),
                 "url": "https://github.com/acme/shop-api/pull/8", "author": "dev4-gh"})

# Releases on main roughly weekly; main merged back into production and integration after each
for k, d in enumerate((69, 62, 54, 47, 40, 32, 25, 17, 10, 6)):
    when = ago(d, 2)
    ver = f"2.{3 + k // 4}.{k % 4}"
    events["main"].append({"kind": "release", "date": iso(when), "sha": sha(), "version": ver,
                           "author": "Lead", "subject": f"Release {ver}"})
    events["production"].append({"kind": "merge", "date": iso(when + timedelta(hours=1)), "sha": sha(), "src": "main",
                                 "pr": None, "author": "Lead", "subject": "Merge branch 'main' into production", "missing": []})
    if d > 9:
        events["integration"].append({"kind": "merge", "date": iso(when + timedelta(hours=4)), "sha": sha(), "src": "main",
                                      "pr": None, "author": "Lead", "subject": "Merge main into integration", "missing": []})
for t in events:
    events[t].sort(key=lambda e: e["date"], reverse=True)

waiting = next(b for b in branches if b["in"].get("production") and not b["in"].get("main"))
idle_b = next(b for b in branches if b.get("stale"))
empty_b = next(b for b in branches if b["empty"])
data = {
    "version": 2, "generated": iso(NOW), "repo": "acme/shop-api", "remote": "origin", "config": CONFIG,
    "trunks": {t: {"tip": sha(), "date": iso(NOW), "events": ev} for t, ev in events.items()},
    "sync": [
        {**CONFIG["flows"][0], "count": waiting["commits"], "commits": []},
        {**CONFIG["flows"][1], "count": 0, "commits": []},
        {**CONFIG["flows"][2], "count": 5, "commits": []},
    ],
    "branches": branches, "fork_prs": fork_prs,
    "stale_unmerged": [{"name": f"branch_old_{i}", "last": iso(ago(d)), "lane": lane, "age_days": d}
                       for i, (d, lane) in enumerate(((41, "dev2"), (58, "dev1"), (96, "dev3"), (133, "dev2")), start=1)],
    "history": [
        {"at": iso(ago(0, 1)), "kind": "pr", "branch": "", "lane": "dev4", "text": f"New PR #8 {fork_prs[-1]['name']} -> integration (Dev4), open"},
        {"at": iso(ago(0, 1)), "kind": "new", "branch": "", "lane": "dev2", "text": f"New branch {empty_b['name']} (Dev2), no commits yet"},
        {"at": iso(ago(1, 4)), "kind": "merged", "branch": "", "lane": "dev1", "text": f"{waiting['name']} (Dev1) merged into production"},
        {"at": iso(ago(1, 4)), "kind": "sync", "branch": "", "lane": "", "text": f"production has {waiting['commits']} commits not in main"},
        {"at": iso(ago(5, 2)), "kind": "sync", "branch": "", "lane": "", "text": "main has 5 commits not in integration"},
        {"at": iso(ago(6, 2)), "kind": "trunk", "branch": "", "lane": "main", "text": "Release 2.5.1 on main"},
        {"at": iso(ago(12, 3)), "kind": "stale", "branch": "", "lane": "dev2", "text": f"{idle_b['name']} (Dev2) open with no activity for 7 days"},
    ],
}

tpl = (HERE / "template.html").read_text(encoding="utf-8")
OUT.parent.mkdir(exist_ok=True)
OUT.write_text("<!doctype html>\n" + tpl.replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":"))), encoding="utf-8")
print(f"ok: {OUT}  ({len(branches)} branches, {len(fork_prs)} PRs)")
