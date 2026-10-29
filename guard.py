"""GitHub guard for the Sentinel launch bot: is the code that will run the submitted model still the code the model was
tested on? Watches swarm (the Sentinel task, simulator core, validator, submit code), the engine fork and the asset repo
on their main and Sentinel branches, plus PyPI's engine, and sorts every change it sees into:
  tested    - the result-relevant files are byte for byte the tested commit's;
  expected  - only known-harmless launch edits: a version line, the engine pin moving to the tested 2.0.0.5, the
              swarm-worlds pin moving to a tag whose assets are the tested ones, registry metadata (state, display,
              emission share), or files you accepted with `bot.sh accept`;
  hold      - anything else in a result-relevant file: the bot must NOT submit until you review it;
  absent    - the ref does not carry Sentinel (main before the merge); gone - the branch was deleted.
Docs, tests, CI and lock files never count. Branch heads and tags are read with `git ls-remote` (git's own protocol, outside
the REST API's 60-an-hour limit without a token); the REST API is used only when a branch moves (its file list) and for
the activity alerts. The last known state is kept in guard_state.json, so a restart is never blind.
usage: guard.py report            one check now, printed
       guard.py accept            accept the files currently held (after you reviewed them)
       guard.py build-baseline    (maintainer) rebuild guard_baseline.json.gz from the tested commits"""
import difflib, gzip, json, os, re, subprocess, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE = os.path.join(HERE, "guard_baseline.json.gz")
ACCEPTED = os.path.join(HERE, "accepted.json")
STATE = os.path.join(HERE, "guard_state.json")
TESTED_ENGINE = (2, 0, 0, 5)
TESTED_DRONE_GYM = "2.0.0.3"
DOCS = (r"^docs/", r"^\.github/", r"\.md$", r"(^|/)tests?/", r"^LICENSE", r"\.gitignore$", r"^\.pre-commit")
REPOS = {
    "swarm": dict(repo="swarm-subnet/swarm", tested="3dcf8d6486b6a0105f77cb9809dfd69736b6339f",
                  refs=("main", "feature/ali/solar-patrol"), marker="swarm/challenge_families/solar_patrol/family.py",
                  info=DOCS + (r"^conftest\.py$", r"^pytest\.ini$", r"^contrib/", r"^scripts/", r"^pixi\.lock$",
                               r"^\.markdownlint", r"^min_compute\.yml$", r"^\.env\.example$", r"^MANIFEST\.in$",
                               r"^swarm/submission_template/", r"^swarm/templates/", r"^validator/docs/"),
                  smart={"requirements.txt": "requirements", "pyproject.toml": "version", "swarm/__init__.py": "version",
                         "swarm/domain_model/benchmark_domain_model.schema.json": "schema"}),
    "engine": dict(repo="swarm-subnet/bullet3-swarmfork", tested="398e9f6f8f483d4003fc56a24a1f215633c7898a",
                   refs=("master", "feature/ali/solar-patrol"),
                   marker="src/SharedMemory/plugins/tinyRendererPlugin/SwarmThermal.cpp",
                   info=DOCS + (r"(^|/)unittests/",), smart={"setup.py": "version"}),
    "worlds": dict(repo="swarm-subnet/swarm-worlds", tested="bf629d215f3240c24e850b1c0c517e42793c4b82",
                   refs=("main", "feature/ali/solar-patrol"),
                   marker="swarm_worlds/maps/custom/solar/intruders/intruders.json",
                   info=DOCS + (r"^tools/",), smart={"pyproject.toml": "version"}),
}
SCHEMA_IGNORE = {"description", "display", "name", "family_state", "emissions_state", "emission_allocation",
                 "king_window", "skill_ids", "market_vertical_ids"}
VERSION_LINE = re.compile(r"""^\s*(#|$|__version__\s*=|version\s*=|version=|\s*version\s*=\s*['"])""")


def kind(key, path):
    c = REPOS[key]
    if path in c["smart"]:
        return "smart"
    return "info" if any(re.search(p, path) for p in c["info"]) else "hold"


class GitHub:
    """GitHub REST reads with ETag caching: an unchanged answer (304) is served from the cache for free."""

    def __init__(self):
        self.etag, self.body, self.calls = {}, {}, 0

    def get(self, path, conditional=True, timeout=30):
        url = path if path.startswith("http") else "https://api.github.com/" + path
        req = urllib.request.Request(url, headers={"User-Agent": "sentinel-launch-bot",
                                                   "Accept": "application/vnd.github+json"})
        if os.environ.get("GITHUB_TOKEN") and "api.github.com" in url:
            req.add_header("Authorization", f"token {os.environ['GITHUB_TOKEN']}")
        if conditional and url in self.etag:
            req.add_header("If-None-Match", self.etag[url])
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                self.calls += "api.github.com" in url   # only the REST API has the 60-an-hour limit without a token
                data = r.read().decode()
                body = json.loads(data) if "api.github.com" in url else data
                if r.headers.get("ETag"):
                    self.etag[url], self.body[url] = r.headers["ETag"], body
                return body, True
        except urllib.error.HTTPError as e:
            if e.code == 304 and url in self.body:
                return self.body[url], False
            raise

    def raw(self, repo, sha, path):
        return self.get(f"https://raw.githubusercontent.com/{repo}/{sha}/{path}", conditional=False)[0]

    def ls_remote(self, repo):
        """{ref name: sha} from `git ls-remote`: git's own protocol, outside the REST API's rate limit. Peeled tags
        ('refs/tags/X^{}') name the commit an annotated tag points at."""
        r = subprocess.run(["git", "ls-remote", f"https://github.com/{repo}.git"], capture_output=True, text=True,
                           timeout=30, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        if r.returncode != 0:
            raise RuntimeError(f"git ls-remote failed: {(r.stderr or r.stdout).strip()[:100]}")
        out = {}
        for line in r.stdout.splitlines():
            sha, _, name = line.partition("\t")
            out[name.strip()] = sha.strip()
        return out

    def tree(self, repo, sha):
        """A commit's file list [{path, sha, type}], read with git (a shallow, blob-free fetch into a small local cache,
        outside the REST rate limit); the REST API only when git fails."""
        cache = os.path.join(HERE, ".gitcache", repo.replace("/", "__") + ".git")
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        try:
            if not os.path.isdir(cache):
                os.makedirs(os.path.dirname(cache), exist_ok=True)
                subprocess.run(["git", "init", "-q", "--bare", cache], check=True, capture_output=True, timeout=30)
            r = subprocess.run(["git", "-C", cache, "fetch", "-q", "--depth", "1", "--filter=blob:none",
                                f"https://github.com/{repo}.git", sha], capture_output=True, text=True, timeout=300, env=env)
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip()[:100])
            r = subprocess.run(["git", "-C", cache, "ls-tree", "-r", "--full-tree", sha], capture_output=True, text=True,
                               timeout=60, check=True)
            out = []
            for line in r.stdout.splitlines():
                meta, _, path = line.partition("\t")
                parts = meta.split()
                if len(parts) == 3:
                    out.append(dict(path=path, sha=parts[2], type=parts[1]))
            return out
        except Exception:
            t = self.get(f"repos/{repo}/git/trees/{sha}?recursive=1", conditional=False)[0]
            if t.get("truncated"):
                raise RuntimeError("tree truncated")
            return t["tree"]


def changed_lines(a, b):
    return [l[2:].rstrip() for l in difflib.ndiff(a.splitlines(), b.splitlines()) if l[:2] in ("- ", "+ ")]


def pins(text):
    out = {}
    for line in text.splitlines():
        s = line.split("#", 1)[0].strip()
        if s:
            name = re.split(r"[=<>!~ @\[;]", s, 1)[0].strip().lower()
            out[name] = s
    return out


class Guard:
    def __init__(self, log=print):
        self.log, self.gh = log, GitHub()
        self.base = json.load(gzip.open(BASELINE, "rt"))
        self.results, self.extra, self.last_ok = {}, {}, 0.0
        self.accepted_mtime, self.worlds_cache = None, {}
        if os.path.exists(STATE):                      # the last known state survives a restart
            try:
                s = json.load(open(STATE))
                self.results = {(k, r): v for k, r, v in s["results"]}
                self.last_ok = float(s.get("last_ok", 0.0))
                self.accepted_mtime = s.get("accepted_mtime")
            except Exception:
                self.results = {}

    def save(self):
        tmp = STATE + ".tmp"
        json.dump(dict(results=[[k, r, v] for (k, r), v in self.results.items()], last_ok=self.last_ok,
                       accepted_mtime=self.accepted_mtime), open(tmp, "w"))
        os.replace(tmp, STATE)

    def unknown(self):
        """Refs never read yet (no state from an earlier run either)."""
        return [f"{k} {r}" for (k, r), v in self.results.items() if not v.get("status")]

    def accepted(self):
        if not os.path.exists(ACCEPTED):
            return set()
        return {tuple(x) for x in json.load(open(ACCEPTED))}

    # ---- smart rules: return (ok, why)
    def smart_ok(self, key, rule, old, new):
        if rule == "version":
            ch = changed_lines(old, new)
            bad = [l for l in ch if not VERSION_LINE.match(l)]
            return (not bad), ("version/comment lines only: " if not bad else "non-version lines: ") + " | ".join((bad or ch)[:6])
        if rule == "requirements":
            a, b = pins(old), pins(new)
            why, ok = [], True
            for name in sorted(set(a) | set(b)):
                if a.get(name) == b.get(name):
                    continue
                spec = b.get(name) or "(removed)"
                if name == "swarm-bullet3" and spec.replace(" ", "") == "swarm-bullet3==2.0.0.5":
                    why.append("engine pin -> 2.0.0.5 (tested)")
                elif name == "swarm-drone-gym" and spec.replace(" ", "") == f"swarm-drone-gym=={TESTED_DRONE_GYM}":
                    why.append("drone-gym pin = tested")
                elif name == "swarm-worlds" and "@" in spec:
                    ref = spec.rsplit("@", 1)[1].strip()
                    good, note = self.worlds_ref_ok(ref)
                    ok &= good
                    why.append(f"swarm-worlds pin -> {ref}: {note}")
                else:
                    ok = False
                    why.append(f"{a.get(name, '(new)')} -> {spec}")
            return ok, "; ".join(why) or "comments only"
        if rule == "schema":
            try:
                A, B = json.loads(old), json.loads(new)
            except ValueError as e:
                return False, f"schema not readable ({e})"
            def part(d):
                fam = {k: v for k, v in d["challenge_families"].get("cf_solar_patrol", {}).items() if k not in SCHEMA_IGNORE}
                return {"family": fam, "interface": d["policy_interfaces"].get("cf_solar_patrol:submission_zip.v1"),
                        "score": d["score_schemas"].get(fam.get("score_schema_id", "ss_solar_patrol_v1"))}
            pa, pb = part(A), part(B)
            diff = [f"{s}.{k}" for s in pa for k in sorted(set(pa[s] or {}) | set(pb[s] or {}))
                    if (pa[s] or {}).get(k) != (pb[s] or {}).get(k)]
            return (not diff), ("Sentinel entries unchanged (metadata / other families only)" if not diff
                                else "Sentinel entries changed: " + ", ".join(diff[:10]))
        return False, f"unknown rule {rule}"

    def worlds_ref_ok(self, ref):
        """Is a swarm-worlds tag / commit the tested assets? (True, note) or (False, note)."""
        c = REPOS["worlds"]
        try:
            sha = None
            if re.fullmatch(r"[0-9a-f]{7,40}", ref):
                sha = ref if len(ref) == 40 else self.gh.get(f"repos/{c['repo']}/commits/{ref}")[0]["sha"]
            else:
                try:                                   # git protocol first: no REST rate limit
                    refs = self.gh.ls_remote(c["repo"])
                    sha = refs.get(f"refs/tags/{ref}^{{}}") or refs.get(f"refs/tags/{ref}")
                except Exception:
                    sha = None
                if sha is None:
                    obj = self.gh.get(f"repos/{c['repo']}/git/ref/tags/{ref}")[0]["object"]
                    if obj["type"] == "tag":
                        obj = self.gh.get(f"repos/{c['repo']}/git/tags/{obj['sha']}")[0]["object"]
                    sha = obj["sha"]
            if sha == c["tested"]:
                return True, "the tested commit"
            if sha in self.worlds_cache:
                return self.worlds_cache[sha]
            holds, _ = self.compare("worlds", sha, self.gh.tree(c["repo"], sha))
            self.worlds_cache[sha] = (not holds), ("assets = tested" if not holds else f"assets differ ({len(holds)} files)")
            return self.worlds_cache[sha]
        except Exception as e:
            return False, f"cannot resolve ({str(e)[:60]})"

    def compare(self, key, sha, tree):
        c, base, acc = REPOS[key], self.base[key], self.accepted()
        cur = {x["path"]: x["sha"] for x in tree if x["type"] == "blob" and kind(key, x["path"]) != "info"}
        holds, expected = [], []
        for p in sorted(set(base["blobs"]) | set(cur)):
            a, b = base["blobs"].get(p), cur.get(p)
            if a == b:
                continue
            if (key, p, b) in acc:
                expected.append(dict(path=p, blob=b, why="accepted by you"))
                continue
            rule = c["smart"].get(p)
            if rule and a and b:
                ok, why = self.smart_ok(key, rule, base["smart"][p], self.gh.raw(c["repo"], sha, p))
                (expected if ok else holds).append(dict(path=p, blob=b, why=why))
            else:
                holds.append(dict(path=p, blob=b, why="added" if not a else "removed" if not b else "changed"))
        return holds, expected

    def check_ref(self, key, ref, heads=None):
        c = REPOS[key]
        if heads is not None:                          # from `git ls-remote`: no REST call for an unchanged branch
            sha = heads.get(f"refs/heads/{ref}")
            if sha is None:
                return dict(status="gone", sha=None)
        else:
            try:
                body, _ = self.gh.get(f"repos/{c['repo']}/git/ref/heads/{ref}")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return dict(status="gone", sha=None)
                raise
            sha = body["object"]["sha"]
        prev = self.results.get((key, ref))
        if prev and prev.get("sha") == sha and not self.reeval:
            return prev
        if sha == c["tested"]:
            return dict(status="tested", sha=sha)
        tree = self.gh.tree(c["repo"], sha)
        if not any(x["path"] == c["marker"] for x in tree):
            return dict(status="absent", sha=sha)
        holds, expected = self.compare(key, sha, tree)
        return dict(status="hold" if holds else "expected" if expected else "tested", sha=sha, holds=holds,
                    expected=expected)

    def engine_pypi(self):
        try:
            v = self.gh.get("https://pypi.org/pypi/swarm-bullet3/json", conditional=False)[0]
            v = json.loads(v)["info"]["version"]
        except Exception as e:
            return dict(status="error", why=str(e)[:60])
        num = tuple(int(x) for x in v.split(".")[:4] if x.isdigit())
        if num <= TESTED_ENGINE:
            return dict(status="tested", version=v)
        for (key, ref), r in self.results.items():   # a newer number is fine only if built from the tested source
            if key == "engine" and r.get("status") in ("tested", "expected") and r.get("sha"):
                try:
                    setup = self.gh.raw(REPOS["engine"]["repo"], r["sha"], "setup.py")
                    if re.search(rf"version\s*=\s*['\"]{re.escape(v)}['\"]", setup):
                        return dict(status="expected", version=v, why=f"setup.py of {ref} (tested source) says {v}")
                except Exception:
                    pass
        return dict(status="hold", version=v, why=f"PyPI engine {v} is newer than the tested 2.0.0.5 and no tested source has it")

    def poll(self):
        """One check of every ref + PyPI. Returns (verdict 'ok'/'hold', list of event lines that changed since the last poll)."""
        mt = os.path.getmtime(ACCEPTED) if os.path.exists(ACCEPTED) else None
        self.reeval, self.accepted_mtime = mt != self.accepted_mtime, mt
        events, fresh, all_ok = [], {}, True
        for key, c in REPOS.items():
            try:
                heads = self.gh.ls_remote(c["repo"])
            except Exception:
                heads = None                           # fall back to the REST API for this repo
            for ref in c["refs"]:
                try:
                    fresh[(key, ref)] = self.check_ref(key, ref, heads)
                except Exception as e:
                    all_ok = False
                    old = {k: v for k, v in self.results.get((key, ref), {}).items() if k != "error"}
                    fresh[(key, ref)] = dict(old, error=str(e)[:80])
        fresh[("pypi", "swarm-bullet3")] = self.engine_pypi()
        for k, r in fresh.items():
            p = self.results.get(k)
            if p is None or p.get("status") != r.get("status") or p.get("sha") != r.get("sha") or p.get("version") != r.get("version"):
                events.append(self.describe(k, r, first=p is None))
        self.results = fresh
        if all_ok:
            self.last_ok = time.time()
        try:
            self.save()
        except OSError:
            pass
        return self.verdict(), [e for e in events if e]

    def verdict(self):
        return "hold" if any(r.get("status") == "hold" for r in self.results.values()) else "ok"

    def describe(self, k, r, first=False):
        key, ref = k
        name = f"{key} {ref}"
        s, sha = r.get("status"), (r.get("sha") or "")[:7]
        if r.get("error") and not s:
            return f"guard: {name}: cannot read ({r['error']})"
        if key == "pypi":
            return f"guard: PyPI swarm-bullet3 {r.get('version')} -> {s.upper()}" + (f" ({r.get('why')})" if r.get("why") else "")
        if s == "hold":
            files = "; ".join(f"{h['path']} [{h['why']}]" for h in r["holds"][:12])
            return f"guard: HOLD {name} {sha}: {len(r['holds'])} result-relevant change(s) not tested: {files}"
        if s == "expected":
            notes = "; ".join(f"{h['path']} [{h['why']}]" for h in r["expected"][:8])
            return f"guard: {name} {sha} = tested code + known launch edits only: {notes}"
        if first and s in ("tested", "absent"):
            return f"guard: {name} {sha}: {'the tested code' if s == 'tested' else 'no Sentinel code on this ref yet'}"
        return f"guard: {name} -> {s} {sha}"

    def report_lines(self):
        out = []
        for (key, ref), r in self.results.items():
            out.append(self.describe((key, ref), r, first=True) or f"guard: {key} {ref}: {r.get('status')}")
        out.append(f"guard verdict: {self.verdict().upper()} (GitHub calls this run: {self.gh.calls})")
        return out

    def accept_current(self):
        acc = self.accepted()
        n = 0
        for (key, ref), r in self.results.items():
            for h in r.get("holds") or []:
                acc.add((key, h["path"], h["blob"]))
                n += 1
        json.dump(sorted(acc), open(ACCEPTED, "w"), indent=1)
        return n


def build_baseline():
    gh, out = GitHub(), {}
    for key, c in REPOS.items():
        tree = gh.get(f"repos/{c['repo']}/git/trees/{c['tested']}?recursive=1", conditional=False)[0]
        assert not tree.get("truncated"), key
        blobs = {x["path"]: x["sha"] for x in tree["tree"] if x["type"] == "blob" and kind(key, x["path"]) != "info"}
        smart = {p: gh.raw(c["repo"], c["tested"], p) for p in c["smart"] if p in blobs}
        out[key] = dict(tested=c["tested"], blobs=blobs, smart=smart)
        print(f"{key}: {len(blobs)} result-relevant files, smart {sorted(smart)}")
    with gzip.open(BASELINE, "wt") as f:
        json.dump(out, f, sort_keys=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "build-baseline":
        build_baseline()
    elif cmd in ("report", "accept"):
        g = Guard()
        g.poll()
        for line in g.report_lines():
            print(line)
        if cmd == "accept":
            n = g.accept_current()
            print(f"accepted {n} held file(s) -> {ACCEPTED}; the bot treats exactly these file versions as reviewed")
    else:
        print(__doc__)
        sys.exit(1)
