#!/usr/bin/env bash
# One-time VPS setup for the Sentinel launch bot: the target model's repo (target.env), the Swarm code the model was
# tested on (3dcf8d6), and a venv with only the packages `swarm model verify` / `swarm model submit` load (no simulator
# engine, no torch). Ends with the official check of the target zip. Safe to run again.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
set -a; . "$HERE/target.env"; [ -f "$HERE/target.local.env" ] && . "$HERE/target.local.env"; set +a
ENV=${SENTINEL_ENV:-$HOME/sentinel_env}; CODE=${SENTINEL_CODE:-$HOME/swarm_sentinel}
COMMIT=3dcf8d6486b6a0105f77cb9809dfd69736b6339f
case "$TARGET_ZIP" in /*) ZIP=$TARGET_ZIP ;; *) ZIP=$TARGET_DIR/$TARGET_ZIP ;; esac
case "$TARGET_SHA_FILE" in /*) SHAF=$TARGET_SHA_FILE ;; *) SHAF=$TARGET_DIR/$TARGET_SHA_FILE ;; esac

PY=$(command -v python3.11 || command -v python3.12 || true)
[ -n "$PY" ] || { echo "python3.11 (or 3.12) is missing: sudo apt install -y python3.11 python3.11-venv git"; exit 1; }
echo "python: $PY"

echo "== target $TARGET_NAME -> $ZIP"
if [ -n "${TARGET_REPO:-}" ]; then                 # a repo target: clone it, or refresh the clone on disk
  if [ -d "$TARGET_DIR/.git" ]; then git -C "$TARGET_DIR" pull -q --ff-only 2>/dev/null || echo "(pull failed; using the copy on disk)"
  else git clone -q "$TARGET_REPO" "$TARGET_DIR" || { echo "cannot clone $TARGET_REPO (private repo without GitHub sign-in?)."
    echo "Copy the zip to the VPS yourself and run: bash bot.sh target $TARGET_NAME /path/to/submission.zip"; exit 1; }; fi
fi                                                 # a local target (bot.sh target NAME PATH): the file is already here
[ -f "$ZIP" ] || { echo "the zip $ZIP is not on this machine"; exit 1; }
TARGET_SHA=$(awk '{print $1; exit}' "$SHAF" 2>/dev/null || true)
[ -n "$TARGET_SHA" ] || { echo "no sha256 in $SHAF"; exit 1; }
got=$(sha256sum "$ZIP" | awk '{print $1}')
[ "$got" = "$TARGET_SHA" ] || { echo "zip sha256 $got is not $TARGET_NAME's $TARGET_SHA"; exit 1; }
echo "$TARGET_NAME zip OK (${TARGET_SHA:0:8})"

echo "== swarm code ${COMMIT:0:7} -> $CODE"
[ -d "$CODE/.git" ] || git clone -q --filter=blob:none https://github.com/swarm-subnet/swarm.git "$CODE"
git -C "$CODE" fetch -q origin feature/ali/solar-patrol
git -C "$CODE" -c advice.detachedHead=false checkout -q "$COMMIT"
git -C "$CODE" log --oneline -1

echo "== python env -> $ENV"
[ -x "$ENV/bin/python" ] || "$PY" -m venv "$ENV"
"$ENV/bin/pip" install -q --upgrade pip
"$ENV/bin/pip" install -q -r "$HERE/requirements_submit.txt"
"$ENV/bin/pip" install -q --no-deps -e "$CODE"

echo "== official check of the $TARGET_NAME zip"
cd "$CODE"
"$ENV/bin/swarm" model verify --model "$ZIP" --max-uncompressed-mb 50 | tee "$HERE/verify.log"
if grep -q "Compliant: True" "$HERE/verify.log" && grep -q "Runtime smoke: True" "$HERE/verify.log"; then
  echo "READY: the VPS can check and submit $TARGET_NAME"
else
  echo "NOT READY: the check above failed"; exit 1
fi
