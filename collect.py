"""Branch Flow collector.

Reads a local clone + GitHub (PRs, branch pushes) and writes:
  data.json     - snapshot the page renders
  history.json  - rolling log of changes between runs
  index.html    - template.html with the snapshot embedded
  changes.txt   - what changed since the previous run (empty = nothing)

Run:  python -I collect.py [--config path/to/config.json]
"""
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_VERSION = 2
OTHER = {"id": "_other", "name": "Others", "note": "unmatched authors", "match": []}
US = "\x1f"
NOW = datetime.now(timezone.utc)

DEFAULTS = {
    "remote": "origin",
    "window_days": 75,         # timeline span kept in the snapshot
    "stale_days": 7,           # open branch with no activity for this long = stale
    "abandoned_days": 30,      # open branch idle longer than this is listed, not drawn
    "stale_list_days": 240,    # older unmerged branches listed up to this age
    "release_pattern": None,   # commit subject on a release trunk that marks a release
    "ignore_pattern": None,    # trunk commits to leave off the timeline (version bumps etc.)
    "version_pattern": r"\d+(?:\.\d+)+",
    "flows": [],
    "people": [],
}


def load_config():
    path = HERE / "config.json"
    if "--config" in sys.argv:
        path = Path(sys.argv[sys.argv.index("--config") + 1]).resolve()
    if not path.exists():
        sys.exit(f"No config at {path}. Copy config.example.json to config.json and edit it.")
    cfg = {**DEFAULTS, **json.loads(path.read_text(encoding="utf-8"))}
    for key in ("repo", "clone", "trunks"):
        if not cfg.get(key):
            sys.exit(f"config: '{key}' is required")
    cfg["base"] = cfg.get("base") or cfg["trunks"][0]["branch"]
    return cfg


CFG = load_config()
REPO, GH_REPO, REMOTE = CFG["clone"], CFG["repo"], CFG["remote"]
TRUNKS = [t["branch"] for t in CFG["trunks"]]
PEOPLE = CFG["people"]
MATCH = [(re.compile("|".join(map(re.escape, p["match"])), re.I), p["id"]) for p in PEOPLE if p.get("match")]
RELEASE_RX = re.compile(CFG["release_pattern"]) if CFG["release_pattern"] else None
IGNORE_RX = re.compile(CFG["ignore_pattern"]) if CFG["ignore_pattern"] else None
VERSION_RX = re.compile(CFG["version_pattern"])


def git(*args, check=True):
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def rref(branch):
    return f"{REMOTE}/{branch}"


def is_ancestor(a, b):
    return subprocess.run(["git", "-C", REPO, "merge-base", "--is-ancestor", a, b],
                          capture_output=True).returncode == 0


def lane_of(*names):
    """First person whose match list hits any of the names (GitHub login or git author)."""
    for n in names:
        for rx, lane in MATCH:
            if n and rx.search(n):
                return lane
    return OTHER["id"]


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso(d):
    return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------- merge subject parsing ----------
MERGE_RX = [
    re.compile(r"^Merge (?:remote-tracking )?branch '(?:[\w-]+/)?(?P<src>[^']+)'(?: of \S+)?(?: into (?P<dst>\S+))?"),
    re.compile(r"^Merge pull request #(?P<pr>\d+) from [^/\s]+/(?P<src>\S+)"),
    re.compile(r"^Merge PR #(?P<pr>\d+) from (?P<src>\S+)(?: into (?P<dst>\S+))?"),
    re.compile(r"^Merge (?:[\w-]+/)?(?P<src>[\w./-]+) into (?P<dst>[\w./-]+)"),
]


def parse_merge(subject):
    for rx in MERGE_RX:
        m = rx.match(subject)
        if m:
            d = m.groupdict()
            src = d.get("src")
            if src and src.startswith(REMOTE + "/"):
                src = src[len(REMOTE) + 1:]
            return src, d.get("pr"), d.get("dst")
    return None, None, None


def trunk_events(trunk, since, reach):
    """Merges into a trunk, plus releases when the trunk is the release trunk."""
    conf = next(t for t in CFG["trunks"] if t["branch"] == trunk)
    out = git("log", "--first-parent", f"--since={since}", f"--format=%H{US}%P{US}%cI{US}%an{US}%s", rref(trunk))
    events = []
    for line in out.splitlines():
        sha, parents, date, author, subj = line.split(US, 4)
        is_release = bool(RELEASE_RX and RELEASE_RX.search(subj))
        if (IGNORE_RX and IGNORE_RX.search(subj)) or (is_release and not conf.get("releases")):
            continue
        if is_release:
            m = VERSION_RX.search(subj)
            events.append({"kind": "release", "date": date, "sha": sha[:8], "version": m.group(0) if m else "",
                           "author": author, "subject": subj})
            continue
        if len(parents.split()) < 2:
            continue
        src, pr, dst = parse_merge(subj)
        if dst and dst != trunk and dst in TRUNKS:
            continue  # fast-forwarded from another trunk: that merge belongs to the other trunk's lane
        events.append({"kind": "merge", "date": date, "sha": sha[:8], "src": src or "?",
                       "pr": int(pr) if pr else None, "author": author, "subject": subj,
                       "missing": [f["to"] for f in CFG["flows"] if f["from"] == trunk and sha not in reach[f["to"]]]})
    return events


def first_fp_merge(tip, trunk):
    """Oldest first-parent commit of trunk that contains tip (the merge that brought it in)."""
    shas = git("rev-list", "--first-parent", "--ancestry-path", f"{tip}..{rref(trunk)}", check=False).split()
    if not shas:
        return None
    parents, date = git("show", "-s", f"--format=%P{US}%cI", shas[-1]).split(US)
    return {"sha": shas[-1], "p1": parents.split()[0], "date": date}


def commits_between(base_args, tip):
    out = git("log", "--no-merges", f"--format=%aI{US}%an{US}%s", tip, *base_args, check=False)
    return [tuple(line.split(US, 2)) for line in out.splitlines()]


def gh_lines(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        print(f"warn: gh {args[0]} {args[1] if len(args) > 1 else ''} failed: {r.stderr.strip()}", file=sys.stderr)
        return None
    return r.stdout


def branch_creators():
    """branch -> (login, pushed_at) of its latest creation push. Git cannot tell who pushed; GitHub can."""
    out = gh_lines("api", "--paginate",
                   f"repos/{GH_REPO}/activity?activity_type=branch_creation&time_period=quarter&per_page=100",
                   "--jq", '.[] | [.ref, .actor.login, .timestamp] | @tsv') or ""
    res = {}
    for line in out.splitlines():
        ref, login, ts = line.split("\t")
        res.setdefault(ref.removeprefix("refs/heads/"), (login, ts))   # newest first
    return res


def trunk_pushes(trunk):
    """[(timestamp, after_sha)] newest first, for pushes to a trunk branch."""
    out = gh_lines("api", "--paginate", f"repos/{GH_REPO}/activity?ref=refs/heads/{trunk}&time_period=quarter&per_page=100",
                   "--jq", '.[] | [.timestamp, .after] | @tsv') or ""
    rows = [tuple(l.split("\t")) for l in out.splitlines() if l.strip()]
    return sorted(((ts, after) for ts, after in rows if after.strip("0")), reverse=True)


def prs():
    out = gh_lines("pr", "list", "--repo", GH_REPO, "--state", "all", "--limit", "100",
                   "--json", "number,title,author,headRefName,baseRefName,state,isDraft,createdAt,mergedAt,"
                             "closedAt,url,isCrossRepository,updatedAt,reviewDecision")
    return json.loads(out) if out else []


def main():
    subprocess.run(["git", "-C", REPO, "fetch", REMOTE, "--prune", "--quiet"], check=False)
    since_dt = NOW - timedelta(days=CFG["window_days"])
    since = iso(since_dt)
    scan_since = NOW - timedelta(days=CFG["window_days"] + 45)

    reach = {t: set(git("rev-list", f"--since={since}", rref(t)).split()) for t in TRUNKS}
    trunks = {t: {"tip": git("rev-parse", "--short=8", rref(t)),
                  "date": git("log", "-1", "--format=%cI", rref(t)),
                  "events": trunk_events(t, since, reach)} for t in TRUNKS}

    def count(rng):
        return int(git("rev-list", "--count", "--no-merges", rng) or 0)

    def subjects(rng, n=12):
        out = git("log", "--no-merges", f"-{n}", "--date=short", f"--format=%ad{US}%an{US}%s", rng)
        return [dict(zip(("date", "author", "subject"), l.split(US, 2))) for l in out.splitlines()]

    sync = []
    for f in CFG["flows"]:
        rng = f"{rref(f['to'])}..{rref(f['from'])}"
        sync.append({**f, "count": count(rng), "commits": subjects(rng)})

    pr_list = prs()
    pr_by_head = {}
    for p in pr_list:
        if not p["isCrossRepository"]:
            pr_by_head.setdefault(p["headRefName"], p)

    data_file = HERE / "data.json"
    prev = json.loads(data_file.read_text(encoding="utf-8")) if data_file.exists() else None
    if prev and prev.get("version") != DATA_VERSION:
        prev = None
    cache = {b["name"]: b for b in prev["branches"]} if prev else {}

    merged_into = {t: set(git("for-each-ref", f"--merged={rref(t)}", "--format=%(refname:lstrip=3)",
                              f"refs/remotes/{REMOTE}").split()) for t in TRUNKS}
    creators = branch_creators()
    pushes = {t: trunk_pushes(t) for t in TRUNKS}

    def was_on_trunk(sha, when):
        """True when sha was already reachable from a trunk at that moment."""
        for rows in pushes.values():
            before = [after for ts, after in rows if parse_iso(ts) <= when]
            if before and is_ancestor(sha, before[0]):
                return True
        return False

    refs = git("for-each-ref", f"refs/remotes/{REMOTE}",
               f"--format=%(refname:lstrip=3){US}%(objectname){US}%(committerdate:iso-strict){US}%(authorname)")
    branches, stale = [], []
    for line in refs.splitlines():
        name, sha, date, tip_author = line.split(US, 3)
        if name in TRUNKS or name == "HEAD":
            continue
        tip_dt = parse_iso(date)
        creator = creators.get(name)          # (login, pushed_at); None when older than GitHub keeps
        made_dt = parse_iso(creator[1]) if creator else None
        seen_dt = max(tip_dt, made_dt) if made_dt else tip_dt   # last sign of life: commit or branch push
        hint = lane_of(creator[0] if creator else None, tip_author)
        flags = {t: name in merged_into[t] for t in TRUNKS}
        merged_any = any(flags.values())

        if seen_dt < scan_since:
            if not merged_any and tip_dt > NOW - timedelta(days=CFG["stale_list_days"]):
                stale.append({"name": name, "last": date, "lane": hint, "age_days": (NOW - tip_dt).days})
            continue

        old = cache.get(name)
        if old and old["tip"] == sha[:8] and old["status"] == "merged" and not old.get("empty"):
            old["in"] = flags      # merged and unchanged: history is fixed, refresh only the trunk flags
            if creator:
                old["lane"], old["creator"] = lane_of(creator[0]), creator[0]
            branches.append(old)
            continue

        if not merged_any and (NOW - seen_dt).days > CFG["abandoned_days"]:
            stale.append({"name": name, "last": date, "lane": hint, "age_days": (NOW - seen_dt).days})
            continue

        merges = []
        for t in TRUNKS:
            if flags[t]:
                m = first_fp_merge(sha, t)
                if m:
                    merges.append((parse_iso(m["date"]), t, m))
        merges.sort(key=lambda x: x[0])
        merges = merges[:1]

        if merges:
            base_args = [f"^{merges[0][2]['p1']}"]
        else:
            base_args = ["--not", *map(rref, TRUNKS)]
        commits = commits_between(base_args, sha)

        empty = not commits
        if empty and made_dt is not None and was_on_trunk(sha, made_dt):
            # pushed from a commit already on a trunk, nothing committed since: open, not merged
            merges = []
            if (NOW - made_dt).days > CFG["abandoned_days"]:
                stale.append({"name": name, "last": creator[1], "lane": hint, "age_days": (NOW - made_dt).days})
                continue
        elif merged_any and not merges:
            # reached a trunk through a path the first-parent search misses; date it by its tip
            merges = [(tip_dt, next(t for t in TRUNKS if flags[t]), {"sha": sha, "p1": sha})]

        created = min((parse_iso(c[0]) for c in commits), default=made_dt or tip_dt)
        if creator:
            lane = lane_of(creator[0], tip_author)
        elif commits:
            lane = Counter(lane_of(c[1]) for c in commits).most_common(1)[0][0]
        else:
            lane = lane_of(tip_author)

        merged_at = merges[0][0] if merges else None
        if (merged_at or NOW) < since_dt and created < since_dt:
            continue  # whole life before the window

        pr = pr_by_head.get(name)
        b = {
            "name": name, "lane": lane, "tip": sha[:8], "created": iso(created), "last": date,
            "commits": len(commits), "empty": empty,
            "first_commit": commits[-1][2] if commits else "",
            "last_commit": commits[0][2] if commits else "",
            "in": flags,
            "merges": [{"into": t, "date": iso(d), "sha": m["sha"][:8]} for d, t, m in merges],
            "status": "merged" if merges else "open",
            "pr": {"number": pr["number"], "state": pr["state"], "url": pr["url"], "draft": pr.get("isDraft"),
                   "review": pr.get("reviewDecision")} if pr else None,
            "creator": creator[0] if creator else None,
        }
        if not merges:
            b["behind_base"] = count(f"{sha}..{rref(CFG['base'])}")
            b["idle_days"] = (NOW - seen_dt).days
            b["stale"] = b["idle_days"] >= CFG["stale_days"]
        branches.append(b)

    # Fork PRs appear only through GitHub.
    fork_prs = []
    for p in pr_list:
        if not p["isCrossRepository"]:
            continue
        end = p["mergedAt"] or p["closedAt"]
        if end and parse_iso(end) < since_dt:
            continue
        fork_prs.append({
            "name": p["headRefName"], "lane": lane_of(p["author"]["login"]), "number": p["number"],
            "title": p["title"], "base": p["baseRefName"], "state": p["state"], "draft": p.get("isDraft"),
            "review": p.get("reviewDecision"), "created": p["createdAt"], "merged": p["mergedAt"],
            "closed": p["closedAt"], "updated": p["updatedAt"], "url": p["url"], "author": p["author"]["login"],
        })

    used = {b["lane"] for b in branches} | {p["lane"] for p in fork_prs} | {s["lane"] for s in stale}
    people = [{k: p[k] for k in ("id", "name", "note") if k in p} for p in PEOPLE]
    if OTHER["id"] in used:
        people.append({k: OTHER[k] for k in ("id", "name", "note")})

    data = {
        "version": DATA_VERSION, "generated": iso(NOW), "repo": GH_REPO, "remote": REMOTE,
        "config": {"trunks": CFG["trunks"], "people": people, "base": CFG["base"], "flows": CFG["flows"],
                   "window_days": CFG["window_days"], "stale_days": CFG["stale_days"],
                   "abandoned_days": CFG["abandoned_days"]},
        "trunks": trunks, "sync": sync, "branches": branches, "fork_prs": fork_prs,
        "stale_unmerged": sorted(stale, key=lambda s: s["age_days"]),
    }

    # ---------- diff vs previous run ----------
    hist_path = HERE / "history.json"
    history = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else []
    changes = diff(prev, data) if prev else []
    for c in changes:
        c["at"] = data["generated"]
    history = (changes + history)[:300]
    data["history"] = history

    data_file.write_text(json.dumps(data, indent=1), encoding="utf-8")
    hist_path.write_text(json.dumps(history, indent=1), encoding="utf-8")
    (HERE / "changes.txt").write_text("\n".join(c["text"] for c in changes), encoding="utf-8")

    tpl = (HERE / "template.html").read_text(encoding="utf-8")
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    (HERE / "index.html").write_text("<!doctype html>\n" + tpl.replace("/*__DATA__*/null", blob), encoding="utf-8")

    open_n = sum(1 for b in branches if b["status"] == "open")
    print(f"ok: {len(branches)} branches ({open_n} open), {len(fork_prs)} fork PRs, "
          f"{len(changes)} changes since last run")
    for c in changes:
        print(" -", c["text"])


def diff(prev, cur):
    names = {p["id"]: p["name"] for p in cur["config"]["people"]}
    out = []

    def add(kind, branch, lane, text):
        out.append({"kind": kind, "branch": branch, "lane": lane, "text": text})

    pb = {b["name"]: b for b in prev.get("branches", [])}
    for b in cur["branches"]:
        old = pb.get(b["name"])
        who = names.get(b["lane"], b["lane"])
        if not old or (old["status"] != "open" and b["status"] == "open"):
            if b["status"] == "open":
                add("new", b["name"], b["lane"],
                    f"New branch {b['name']} ({who}), {'no commits yet' if b['empty'] else 'not merged'}")
            continue
        if old["status"] == "open" and b["status"] == "merged":
            add("merged", b["name"], b["lane"], f"{b['name']} ({who}) merged into {b['merges'][0]['into']}")
        for t, now_in in b["in"].items():
            first_merge = b["merges"] and b["merges"][0]["into"] == t and old["status"] == "open"
            if now_in and not old["in"].get(t) and not first_merge:
                add("promoted", b["name"], b["lane"], f"{b['name']} ({who}) now in {t}")
        if b["status"] == "open" and b.get("stale") and not old.get("stale"):
            add("stale", b["name"], b["lane"], f"{b['name']} ({who}) open with no activity for {b['idle_days']} days")
    gone = set(pb) - {b["name"] for b in cur["branches"]} - {s["name"] for s in cur["stale_unmerged"]}
    for n in sorted(gone):
        if pb[n]["status"] == "open":
            add("deleted", n, pb[n]["lane"], f"Open branch {n} was deleted on GitHub")
    pp = {p["number"]: p for p in prev.get("fork_prs", [])}
    for p in cur["fork_prs"]:
        old = pp.get(p["number"])
        who = names.get(p["lane"], p["lane"])
        if not old:
            add("pr", p["name"], p["lane"], f"New PR #{p['number']} {p['name']} -> {p['base']} ({who}), {p['state'].lower()}")
        elif old["state"] != p["state"]:
            add("pr", p["name"], p["lane"], f"PR #{p['number']} {p['name']} ({who}) is now {p['state'].lower()}")
    before = {(s["from"], s["to"]): s["count"] for s in prev.get("sync", [])}
    for s in cur["sync"]:
        a = before.get((s["from"], s["to"]))
        if a is not None and (a == 0) != (s["count"] == 0):
            add("sync", "", "", f"{s['from']} has {s['count']} commits not in {s['to']}" if s["count"]
                else f"{s['to']} has caught up with {s['from']}")
    seen = {e["sha"] for t in prev["trunks"].values() for e in t["events"]}
    for t, info in cur["trunks"].items():
        for e in info["events"]:
            if e["sha"] not in seen:
                if e["kind"] == "release":
                    add("trunk", "", t, f"Release {e['version']} on {t}")
                else:
                    add("trunk", e["src"], t, f"{e['src']} merged into {t}")
    return out


if __name__ == "__main__":
    main()
