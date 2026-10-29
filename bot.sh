#!/usr/bin/env bash
# Sentinel launch bot: one script for everything (run from anywhere: bash ~/sentinel-launch-bot/bot.sh ...).
#   bot.sh setup                         one time: target repo + Swarm code + packages + the official check of the zip
#   bot.sh uid    WALLET HOTKEY          your hotkey's UID, protection left, and whether it already submitted
#   bot.sh check  WALLET HOTKEY          uid + guard + a launch-day rehearsal (never submits)
#   bot.sh start  WALLET HOTKEY          run the bot in the background, LIVE: it submits the target ONCE when Sentinel
#                                        opens, unless the GitHub guard holds; restarts itself after a crash
#   bot.sh status                        running? guard verdict, submit state, last log lines
#   bot.sh log                           follow the log (Ctrl-C leaves the log; the bot keeps running)
#   bot.sh guard                         one GitHub guard check now, every repo and branch, with the held files
#   bot.sh accept                        after reviewing a HOLD: accept exactly the held file versions (the bot goes on)
#   bot.sh stop                          stop the bot
#   bot.sh reboot-on | reboot-off        also start the bot again after a VPS reboot (crontab @reboot)
# Settings: target.env (which model), bot.env (optional, never committed: NOTIFY_DISCORD_WEBHOOK=..., GITHUB_TOKEN=...).
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ENV=${SENTINEL_ENV:-$HOME/sentinel_env}
PIDF=$HERE/bot.pid; CONF=$HERE/bot.conf; LOGF=$HERE/bot.log; OUT=$HERE/bot.out
set -a; . "$HERE/target.env"; [ -f "$HERE/bot.env" ] && . "$HERE/bot.env"
TARGET_SHA=$(awk '{print $1; exit}' "$TARGET_DIR/$TARGET_SHA_FILE" 2>/dev/null || true); set +a

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

  log)
    tail -n 40 -f "$LOGF" ;;

  guard)
    (cd "$HERE" && PY guard.py report) ;;

  accept)
    echo "This accepts exactly the file versions held right now as reviewed. Only do this after reading them."
    read -r -p "Type ACCEPT to continue: " a; [ "$a" = "ACCEPT" ] || { echo "nothing accepted"; exit 1; }
    (cd "$HERE" && PY guard.py accept) ;;

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
