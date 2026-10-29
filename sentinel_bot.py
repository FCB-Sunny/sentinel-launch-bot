"""Swarm Sentinel launch bot. Submits the target model (target.env) ONCE, as soon as Sentinel opens, and only while the
code that will run it is the code it was tested on.
  - official backend (api.swarm124.com), every 15 s: the ONLY trigger. Sentinel is open when cf_solar_patrol is in the
    backend's family list (the list `swarm model submit` checks) and the submission window is open;
  - GitHub guard (guard.py), every 60 s: swarm / engine / assets on their main and Sentinel branches, plus PyPI's engine.
    A result-relevant change that was not tested puts the bot on HOLD: it alerts and does NOT submit until you review
    it (`bot.sh guard`, then `bot.sh accept` if it is fine). Known launch edits (merges of the tested code, the engine
    pin moving to 2.0.0.5, version lines, registry metadata) are reported and do not hold;
  - other GitHub activity, every 10 min: new or updated pull requests, new tags, new repos (alerts only).
A failed commit is retried; once the commit is on-chain only the upload is retried (--upload-only), so a restart never
commits twice. Log: bot.log; state: bot_state.json. NOTIFY_DISCORD_WEBHOOK (bot.env) gets the important lines.
Exit: 0 submitted (or already), 3 stopped for good (wrong zip, hotkey not ready, refused); anything else = crash.
usage: sentinel_bot.py --wallet NAME --hotkey NAME [--live] [--allow-code-change] [--pretend-live]
  --pretend-live : act as if Sentinel were live, run every check, never submit (a rehearsal)
  --allow-code-change : submit even while the guard holds (not recommended)"""
import argparse, hashlib, json, os, subprocess, sys, time, urllib.request

import guard as G

HERE = os.path.dirname(os.path.abspath(__file__))
LOG, STATE = os.path.join(HERE, "bot.log"), os.path.join(HERE, "bot_state.json")
STATUS = os.path.join(HERE, "bot_status.json")
ENV = os.environ.get("SENTINEL_ENV", os.path.expanduser("~/sentinel_env"))
CODE = os.environ.get("SENTINEL_CODE", os.path.expanduser("~/swarm_sentinel"))
NAME = os.environ.get("TARGET_NAME", "?")
ZIP = os.path.expanduser(os.path.join(os.environ.get("TARGET_DIR", ""), os.environ.get("TARGET_ZIP", "")))
SHA = os.environ.get("TARGET_SHA", "")
API, FAMILY = "https://api.swarm124.com", "cf_solar_patrol"
FATAL = ("already holds a submission", "already registered to UID", "same model as an existing", "Wallet error")
BACKEND_S, GUARD_S, ACTIVITY_S, BEAT_S, STALE_S = 15, 60, 600, 900, 600
STOP = 3
_last_err = 0.0


def log(msg, notify=False):
    line = time.strftime("%Y-%m-%d %H:%M:%S %Z ") + msg
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")
    hook = os.environ.get("NOTIFY_DISCORD_WEBHOOK")
    if notify and hook:
        try:
            body = json.dumps({"content": f"[Sentinel bot] {msg}"[:1900]}).encode()
            req = urllib.request.Request(hook, data=body, headers={"Content-Type": "application/json",
                                                                   "User-Agent": "sentinel-launch-bot"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print(f"notify failed: {str(e)[:80]}", flush=True)


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "sentinel-launch-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"submitted": False, "committed": False, "attempts": 0}


def save_state(s):
    json.dump(s, open(STATE, "w"), indent=1)


def write_status(phase, mode, live, last, verdict, guard, state):
    """bot_status.json for `bot.sh live`: what the bot is doing right now (rewritten every loop, ~5 s)."""
    refs = []
    for (key, ref), r in guard.results.items():
        tag = r.get("version") or (r.get("sha") or "")[:7]
        refs.append(f"{key} {ref}: {r.get('status') or 'unknown'} {tag}" + (f" ({len(r['holds'])} held files)" if r.get("holds") else "")
                    + (f"  [last read failed: {r['error'][:40]}]" if r.get("error") else ""))
    s = dict(time=time.time(), phase=phase, mode=mode, target=f"{NAME} {SHA[:8]}", live=live,
             backend_t=last["backend"], guard_t=last["guard"], activity_t=last["activity"], verdict=verdict,
             refs=refs, gh_calls=guard.gh.calls, submitted=state.get("submitted"), committed=state.get("committed"),
             attempts=state.get("attempts"))
    tmp = STATUS + ".tmp"
    json.dump(s, open(tmp, "w"), indent=1)
    os.replace(tmp, STATUS)


def live_now():
    """True / False from the backend's family list and submission window, None when the backend cannot be read."""
    global _last_err
    try:
        fams = get(f"{API}/families/metadata")["challenge_families"]
    except Exception as e:
        if time.time() - _last_err > 300:              # an outage logs once per 5 minutes, not every poll
            log(f"backend error {str(e)[:120]}")
            _last_err = time.time()
        return None
    if FAMILY not in fams:
        return False
    try:
        return bool(get(f"{API}/miners/submission-window").get("open", True))
    except Exception:
        return True                       # the submit command checks the window again itself


class Activity:
    """New or updated pull requests, new tags and new repos in the swarm-subnet org (alerts only)."""

    def __init__(self, gh):
        self.gh, self.seen = gh, None

    def snapshot(self):
        snap = {}
        for key, c in G.REPOS.items():
            prs, _ = self.gh.get(f"repos/{c['repo']}/pulls?state=open&per_page=50")
            for p in prs:
                snap[f"{key} PR #{p['number']}"] = f"{p['title'][:70]} ({p['head']['ref']} {p['head']['sha'][:7]} -> {p['base']['ref']})"
            tags, _ = self.gh.get(f"repos/{c['repo']}/tags?per_page=10")
            for t in tags:
                snap[f"{key} tag {t['name']}"] = t["commit"]["sha"][:7]
        repos, _ = self.gh.get("orgs/swarm-subnet/repos?per_page=100")
        for r in repos:
            snap[f"repo {r['name']}"] = "exists"
        return snap

    def check(self):
        try:
            snap = self.snapshot()
        except Exception as e:
            return [f"activity: cannot read GitHub ({str(e)[:80]})"], False
        if self.seen is None:
            self.seen = snap
            return [f"activity baseline: {len(snap)} open PRs / tags / repos"], False
        new = [f"{k}: {v}" for k, v in snap.items() if self.seen.get(k) != v]
        gone = [k for k in self.seen if k not in snap]
        self.seen = snap
        lines = [f"GITHUB ACTIVITY (new/updated): {x}" for x in new] + [f"GITHUB ACTIVITY (closed/removed): {x}" for x in gone]
        return lines, bool(lines)


def hotkey_check(wallet, hotkey, *extra):
    r = subprocess.run([f"{ENV}/bin/python", os.path.join(HERE, "hotkey_check.py"), wallet, hotkey, *extra],
                       capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    for line in (r.stdout + r.stderr).strip().splitlines()[-6:]:
        log(f"hotkey: {line}")
    return r.returncode


def submit(args, state):
    upload_only = state["committed"]
    if not upload_only and hotkey_check(args.wallet, args.hotkey, "--committed", SHA) == 0:
        state["committed"] = upload_only = True    # an earlier run's commit landed: only the upload is left
        save_state(state)
    cmd = [f"{ENV}/bin/swarm", "model", "submit", "--artifact", ZIP, "--family-id", FAMILY,
           "--wallet.name", args.wallet, "--wallet.hotkey", args.hotkey, "--netuid", "124", "--subtensor.network", "finney"]
    if upload_only:
        cmd.append("--upload-only")
    log(f"SUBMITTING {NAME}{' (upload only)' if upload_only else ''}: {' '.join(cmd)}", notify=True)
    r = subprocess.run(cmd, cwd=CODE, capture_output=True, text=True, timeout=3600, stdin=subprocess.DEVNULL)
    out = r.stdout + r.stderr
    log(f"submit exit {r.returncode}\n{out[-4000:]}")
    state["attempts"] += 1
    if "PRIVATE MODEL SUBMITTED SUCCESSFULLY" in out or (upload_only and r.returncode == 0):
        state["submitted"] = True
    if "Commitment succeeded" in out or "did not land" in out:
        state["committed"] = True
    save_state(state)
    if not state["submitted"] and any(k in out for k in FATAL):
        log("STOPPED: the backend or wallet refused this submission for good (see bot.log); no more retries.", notify=True)
        sys.exit(STOP)
    if not state["submitted"]:
        log(f"submit attempt {state['attempts']} did not finish; retrying in 30 s", notify=state["attempts"] == 1)
    return state["submitted"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--hotkey", required=True)
    ap.add_argument("--live", action="store_true", help="really submit (default: alert-only)")
    ap.add_argument("--allow-code-change", action="store_true")
    ap.add_argument("--pretend-live", action="store_true")
    args = ap.parse_args()
    if args.pretend_live:
        args.live = False
    mode = "LIVE (will submit)" if args.live else "ALERT-ONLY (never submits)"
    log(f"start: {mode}; target {NAME} {ZIP}; wallet {args.wallet}/{args.hotkey}"
        f"{'; GUARD OFF (--allow-code-change)' if args.allow_code_change else ''}", notify=True)
    if not SHA or not os.path.isfile(ZIP) or sha256(ZIP) != SHA:
        log(f"STOPPED: the target zip {ZIP} is missing, or its sha256 does not match the target repo's sha256 file", notify=True)
        sys.exit(STOP)
    log(f"target zip sha256 OK ({NAME} {SHA[:8]})")
    hk = hotkey_check(args.wallet, args.hotkey)
    if hk == 1 and args.live:
        log("STOPPED: the hotkey is not ready (see bot.log); fix it and start again", notify=True)
        sys.exit(STOP)
    state = load_state()
    if state["submitted"]:
        log("already submitted earlier (bot_state.json); nothing to do")
        return
    guard = G.Guard()
    activity = Activity(guard.gh)
    last = dict(backend=0.0, guard=0.0, activity=0.0, beat=0.0, hold_note=0.0, stale_note=0.0)
    verdict, live = "unknown", False

    def run_guard(first=False):
        nonlocal verdict
        verdict, events = guard.poll()
        for e in events:                               # the first check is the baseline; later lines are real changes
            log(e, notify=not first or "HOLD" in e)
        last["guard"] = time.time()

    write_status("starting: first GitHub guard check", mode, None, last, verdict, guard, state)
    run_guard(first=True)
    log(f"guard verdict: {verdict.upper()}", notify=verdict == "hold")
    alerted = False
    while True:
        now = time.time()
        if now - last["backend"] >= BACKEND_S:
            live = True if args.pretend_live else live_now()
            last["backend"] = now
        if live:
            if now - last["guard"] >= 20:
                run_guard()                                # a fresh guard check (<= 20 s old) before any submit
            stale = time.time() - guard.last_ok > STALE_S
            if stale and now - last["stale_note"] > 300:
                log(f"guard WARNING: GitHub could not be fully read for {int((time.time() - guard.last_ok) / 60)} min; "
                    f"deciding on the last known state{' (never read: ' + ', '.join(guard.unknown()) + ')' if guard.unknown() else ''}",
                    notify=True)
                last["stale_note"] = now
            ok = verdict == "ok" or args.allow_code_change
            if not args.live:
                if not alerted:
                    log("SENTINEL IS LIVE" + (" (pretend)" if args.pretend_live else "") +
                        f" - guard {verdict.upper()}; ALERT-ONLY: {'would submit now' if ok else 'would HOLD'}",
                        notify=not args.pretend_live)
                    alerted = True
                    if args.pretend_live:
                        for line in guard.report_lines():
                            log(line)
                        return
            elif not ok:
                if now - last["hold_note"] > 300:
                    log("SENTINEL IS LIVE but the guard HOLDS: NOT submitting. Review `bash bot.sh guard`; if the "
                        "changes are fine, `bash bot.sh accept` and the bot submits on its next check.", notify=True)
                    last["hold_note"] = now
            else:
                log(f"SENTINEL IS LIVE - guard OK - submitting {NAME}", notify=True)
                write_status("live: SUBMITTING", mode, live, last, verdict, guard, state)
                if submit(args, state):
                    log(f"DONE: {NAME} submitted", notify=True)
                    write_status("DONE: submitted", mode, live, last, verdict, guard, state)
                    return
                time.sleep(30)
                continue
        else:
            if now - last["guard"] >= GUARD_S:
                run_guard()
            if now - last["activity"] >= ACTIVITY_S:
                lines, important = activity.check()
                for l in lines:
                    log(l, notify=important)
                last["activity"] = now
        if now - last["beat"] >= BEAT_S:
            log(f"heartbeat: Sentinel {'live' if live else 'not live yet' if live is False else 'unknown (backend unreachable)'}"
                f"; guard {verdict.upper()}; GitHub calls so far {guard.gh.calls}")
            last["beat"] = now
        phase = ("live: HOLD (not submitting)" if live and verdict == "hold" and not args.allow_code_change
                 else "live" if live else "watching (Sentinel not live yet)" if live is False
                 else "watching (backend unreachable)")
        write_status(phase, mode, live, last, verdict, guard, state)
        time.sleep(5)


if __name__ == "__main__":
    main()
