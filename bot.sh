#!/usr/bin/env bash
# Sentinel launch bot: one script for everything (run from anywhere: bash ~/sentinel-launch-bot/bot.sh ...).
#   bot.sh setup                         one time: target repo + Swarm code + packages + the official check of the zip
#   bot.sh uid    WALLET HOTKEY          your hotkey's UID, protection left, and whether it already submitted
#   bot.sh check  WALLET HOTKEY          uid + guard + a launch-day rehearsal (never submits)
#   bot.sh start  WALLET HOTKEY          run the bot in the background, LIVE: it submits the target ONCE when Sentinel
#                                        opens, unless the GitHub guard holds; restarts itself after a crash
#   bot.sh status                        running? guard verdict, submit state, last log lines
#   bot.sh live                          real-time screen, refreshed every 2 s (Ctrl-C leaves it; the bot keeps running)
#   bot.sh log                           follow the log (Ctrl-C leaves the log; the bot keeps running)
#   bot.sh guard                         one GitHub guard check now, every repo and branch, with the held files
#   bot.sh accept                        after reviewing a HOLD: accept exactly the held file versions (the bot goes on)
#   bot.sh target [NAME PATH|REPO_URL | reset]  show / change the model to submit: a zip or folder already on this
#                                        machine, or a repo to clone (then: setup, check, start)
#   bot.sh update                        update the bot itself from the public repo (no git or sign-in needed)
#   bot.sh stop                          stop the bot
#   bot.sh reboot-on | reboot-off        also start the bot again after a VPS reboot (crontab @reboot)
# Settings: target.env (which model), bot.env (optional, never committed: NOTIFY_DISCORD_WEBHOOK=..., GITHUB_TOKEN=...).
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ENV=${SENTINEL_ENV:-$HOME/sentinel_env}
PIDF=$HERE/bot.pid; CONF=$HERE/bot.conf; LOGF=$HERE/bot.log; OUT=$HERE/bot.out
set -a; . "$HERE/target.env"; [ -f "$HERE/target.local.env" ] && . "$HERE/target.local.env"; [ -f "$HERE/bot.env" ] && . "$HERE/bot.env"
case "$TARGET_ZIP" in /*) ZIPF=$TARGET_ZIP ;; *) ZIPF=$TARGET_DIR/$TARGET_ZIP ;; esac
case "$TARGET_SHA_FILE" in /*) SHAF=$TARGET_SHA_FILE ;; *) SHAF=$TARGET_DIR/$TARGET_SHA_FILE ;; esac
TARGET_SHA=$(awk '{print $1; exit}' "$SHAF" 2>/dev/null || true); set +a

running() { [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; }
need_env() { [ -x "$ENV/bin/python" ] || { echo "no Python env at $ENV: run 'bash bot.sh setup' first"; exit 1; }; }
PY() { if [ -x "$ENV/bin/python" ]; then "$ENV/bin/python" "$@"; else python3 "$@"; fi; }

case "${1:-}" in
  setup)
    bash "$HERE/setup.sh" ;;

  uid)
    need_env; W=${2:?usage: bot.sh uid WALLET HOTKEY}; H=${3:?usage: bot.sh uid WALLET HOTKEY}
    "$ENV/bin/python" "$HERE/hotkey_check.py" "$W" "$H" ;;

  check)
    need_env; W=${2:?usage: bot.sh check WALLET HOTKEY}; H=${3:?usage: bot.sh check WALLET HOTKEY}
    echo "== hotkey"; "$ENV/bin/python" "$HERE/hotkey_check.py" "$W" "$H"
    echo "== rehearsal (never submits)"
    (cd "$HERE" && "$ENV/bin/python" sentinel_bot.py --wallet "$W" --hotkey "$H" --pretend-live) ;;

  start)
    need_env; W=${2:?usage: bot.sh start WALLET HOTKEY}; H=${3:?usage: bot.sh start WALLET HOTKEY}; EXTRA=${4:-}
    if running; then echo "the bot is already running (pid $(cat "$PIDF")); 'bash bot.sh stop' first"; exit 1; fi
    if [ -f "$HERE/bot_state.json" ] && grep -q '"submitted": true' "$HERE/bot_state.json"; then
      echo "already submitted (bot_state.json) - nothing to do"; exit 0
    fi
    "$ENV/bin/python" "$HERE/hotkey_check.py" "$W" "$H" || { echo "the hotkey is not ready (above) - the bot was NOT started"; exit 1; }
    printf 'W=%q\nH=%q\nEXTRA=%q\n' "$W" "$H" "$EXTRA" > "$CONF"
    setsid nohup bash -c '
      ENV=$1; HERE=$2; W=$3; H=$4; EXTRA=$5; cd "$HERE"
      while true; do
        "$ENV/bin/python" sentinel_bot.py --wallet "$W" --hotkey "$H" --live $EXTRA > /dev/null
        rc=$?
        if [ $rc -eq 0 ]; then echo "bot finished: submitted ($(date))"; break; fi
        if [ $rc -eq 3 ]; then echo "bot stopped for good ($(date)); see bot.log"; break; fi
        echo "bot crashed (exit $rc) at $(date); restarting in 20 s"; sleep 20
      done' bot "$ENV" "$HERE" "$W" "$H" "$EXTRA" >> "$OUT" 2>&1 < /dev/null &
    echo $! > "$PIDF"
    sleep 12
    if running; then
      echo "bot STARTED (pid $(cat "$PIDF")), LIVE: it submits $TARGET_NAME once when Sentinel opens and the guard is OK."
      echo "You can log out. Check it with: bash $HERE/bot.sh status"
      tail -n 12 "$LOGF"
    else
      echo "the bot did not stay up:"; tail -n 20 "$OUT"; tail -n 10 "$LOGF"; exit 1
    fi ;;

  status)
    if running; then echo "bot: RUNNING (pid $(cat "$PIDF"))"; else echo "bot: NOT RUNNING"; fi
    [ -f "$CONF" ] && { . "$CONF"; echo "target $TARGET_NAME | wallet $W / hotkey $H ${EXTRA:+($EXTRA)}"; }
    [ -f "$HERE/bot_state.json" ] && python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print("submitted:", s.get("submitted"), "| committed:", s.get("committed"), "| attempts:", s.get("attempts"))' "$HERE/bot_state.json"
    grep -E "guard verdict|guard: HOLD|heartbeat" "$LOGF" 2>/dev/null | tail -n 2
    [ -f "$OUT" ] && tail -n 2 "$OUT"
    echo "--- last log lines"; tail -n 8 "$LOGF" 2>/dev/null ;;

  live)
    trap 'echo; echo "left the live view; the bot keeps running"; exit 0' INT
    while true; do
      screen=$(
        echo "=== Sentinel launch bot - live view ($(date '+%Y-%m-%d %H:%M:%S %Z'))"
        if running; then echo "bot process: RUNNING (pid $(cat "$PIDF"))"; else echo "bot process: NOT RUNNING"; fi
        python3 - "$HERE/bot_status.json" <<'PY'
import json, sys, time
try:
    s = json.load(open(sys.argv[1]))
except Exception:
    print("no status yet (the bot writes it every ~5 s once started)"); sys.exit()
now = time.time()
ago = lambda t: f"{int(now - t)} s ago" if t else "not yet"
print(f"status updated {ago(s['time'])}   (older than ~30 s = the bot is busy or stopped)")
print(f"PHASE:  {s['phase']}")
print(f"mode {s['mode']} | target {s['target']}")
live = {True: "LIVE", False: "not live yet", None: "unknown (unreachable)"}[s["live"]]
print(f"official backend   Sentinel {live:<22} checked {ago(s['backend_t'])}  (every 15 s)")
print(f"GitHub guard       verdict {s['verdict'].upper():<23} checked {ago(s['guard_t'])}  (every 60 s; GitHub calls {s['gh_calls']})")
for r in s["refs"]:
    print(f"    {r}")
print(f"GitHub activity    checked {ago(s['activity_t'])}  (every 10 min)")
print(f"submit             submitted {s['submitted']} | committed {s['committed']} | attempts {s['attempts']}")
PY
        echo "--- last log lines"
        tail -n 12 "$LOGF" 2>/dev/null | cut -c1-200
        echo "(refreshes every 2 s; Ctrl-C leaves this view - the bot keeps running)"
      )
      clear 2>/dev/null || printf '\033[2J\033[H'
      echo "$screen"
      sleep 2
    done ;;

  log)
    tail -n 40 -f "$LOGF" ;;

  guard)
    (cd "$HERE" && PY guard.py report) ;;

  accept)
    echo "This accepts exactly the file versions held right now as reviewed. Only do this after reading them."
    read -r -p "Type ACCEPT to continue: " a; [ "$a" = "ACCEPT" ] || { echo "nothing accepted"; exit 1; }
    (cd "$HERE" && PY guard.py accept) ;;

  target)
    if [ -z "${2:-}" ]; then
      echo "target: $TARGET_NAME | zip $ZIPF | sha256 ${TARGET_SHA:0:8}${TARGET_SHA:+...}${TARGET_REPO:+ | repo $TARGET_REPO}"
      [ -f "$HERE/target.local.env" ] && echo "(set by you in target.local.env; 'bash bot.sh target reset' goes back to the default)"
      [ -f "$ZIPF" ] || echo "WARNING: the zip is not on this machine yet"
      exit 0
    fi
    if running; then echo "stop the bot first: bash bot.sh stop"; exit 1; fi
    if [ -f "$HERE/bot_state.json" ] && grep -q '"committed": true' "$HERE/bot_state.json"; then
      echo "this hotkey already committed a model (bot_state.json): one submission per hotkey, ever."
      echo "A new candidate needs a NEW hotkey (and UID); start the bot with that hotkey after changing the target."
    fi
    if [ "$2" = "reset" ]; then rm -f "$HERE/target.local.env" "$HERE/target.local.sha"; echo "target back to the default in target.env"; exit 0; fi
    N=$2; R=${3:?usage: bash bot.sh target NAME /path/to/submission.zip   (or a folder holding it, or a repo URL)}
    case "$R" in
      http*://*|git@*)                             # a repo to clone (needs read access to it on this machine)
        D=$HOME/$(basename "$R" .git)
        printf 'TARGET_NAME=%s\nTARGET_REPO=%s\nTARGET_DIR=%s\nTARGET_ZIP=Submission/submission.zip\nTARGET_SHA_FILE=Submission/sha256.txt\n' "$N" "$R" "$D" > "$HERE/target.local.env"
        echo "target set to $N ($R -> $D)." ;;
      *)                                           # a zip, or a folder holding it, already on this machine
        P=$(cd "$(dirname "$R")" 2>/dev/null && pwd)/$(basename "$R")
        if [ -d "$P" ]; then
          for z in "$P/Submission/submission.zip" "$P/submission.zip" "$P"/*.zip; do [ -f "$z" ] && { Z=$z; break; }; done
        else Z=$P; fi
        [ -n "${Z:-}" ] && [ -f "$Z" ] || { echo "no zip found at $R"; exit 1; }
        got=$(sha256sum "$Z" | awk '{print $1}')
        SF=""
        for f in "$(dirname "$Z")/sha256.txt" "$(dirname "$Z")/$(basename "$Z").sha256"; do [ -f "$f" ] && { SF=$f; break; }; done
        if [ -n "$SF" ]; then
          want=$(awk '{print $1; exit}' "$SF")
          [ "$got" = "$want" ] || { echo "the zip's sha256 $got does not match $SF ($want): wrong or damaged copy"; exit 1; }
          echo "zip sha256 matches $SF (${got:0:8}...)"
        else
          echo "no sha256 file next to the zip. Its sha256 is:"; echo "  $got"
          read -r -p "Is this the model you mean (compare with the sha you were given)? Type yes: " ok
          [ "$ok" = "yes" ] || { echo "nothing changed"; exit 1; }
          SF=$HERE/target.local.sha; echo "$got  $(basename "$Z")" > "$SF"
        fi
        printf 'TARGET_NAME=%s\nTARGET_REPO=\nTARGET_DIR=%s\nTARGET_ZIP=%s\nTARGET_SHA_FILE=%s\n' "$N" "$(dirname "$Z")" "$Z" "$SF" > "$HERE/target.local.env"
        echo "target set to $N: $Z" ;;
    esac
    echo "Next: bash bot.sh setup   (official check of the zip, cloning first for a repo target; must end with READY)"
    echo "then: bash bot.sh check <WALLET> <HOTKEY>   and   bash bot.sh start <WALLET> <HOTKEY>" ;;

  update)
    if running; then echo "note: the running bot keeps its old code until you restart it (stop, then start)"; fi
    TMP=$(mktemp -d) && curl -fsSL https://github.com/FCB-Sunny/sentinel-launch-bot/archive/refs/heads/main.tar.gz | tar xz -C "$TMP" \
      || { echo "download failed"; rm -rf "$TMP"; exit 1; }
    cp -r "$TMP"/sentinel-launch-bot-main/. "$HERE"/ && rm -rf "$TMP"
    echo "bot updated from the public repo (your bot.env, accepted.json, guard state and target choice are kept)" ;;

  stop)
    if running; then
      if [ -f "$HERE/bot_state.json" ] && grep -q '"committed": true' "$HERE/bot_state.json" && ! grep -q '"submitted": true' "$HERE/bot_state.json"; then
        echo "WARNING: the commit is on-chain but the upload has not landed; start the bot again soon (it resumes with the upload only)"
      fi
      kill -- -"$(cat "$PIDF")" 2>/dev/null; sleep 1; rm -f "$PIDF"; echo "bot stopped"
    else
      echo "the bot is not running"; rm -f "$PIDF"
    fi ;;

  reboot-on)
    [ -f "$CONF" ] || { echo "start the bot once first (bash bot.sh start WALLET HOTKEY)"; exit 1; }
    . "$CONF"
    line="@reboot sleep 60 && bash $HERE/bot.sh start $W $H $EXTRA >> $OUT 2>&1"
    (crontab -l 2>/dev/null | grep -v "$HERE/bot.sh"; echo "$line") | crontab - && echo "added: $line" ;;

  reboot-off)
    (crontab -l 2>/dev/null | grep -v "$HERE/bot.sh") | crontab - && echo "removed the @reboot line" ;;

  *)
    sed -n '2,15p' "$0"; exit 1 ;;
esac
