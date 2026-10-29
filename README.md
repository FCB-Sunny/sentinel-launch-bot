# Sentinel launch bot

Runs on the VPS. It submits the target model (now **v10g**, set in `target.env`) to Swarm Sentinel
(`cf_solar_patrol`) **once, automatically**, the moment Sentinel opens, but **only while the code that will run it is the
code it was tested on**.

| Watches | How often | What it does |
|---|---|---|
| Official backend `api.swarm124.com` | every 15 s | The **only trigger**. Sentinel is open when `cf_solar_patrol` is in the backend's family list (the list `swarm model submit` checks) and the submission window is open. |
| GitHub guard: swarm, engine fork, assets repo (main + Sentinel branches), PyPI engine | every 60 s | Compares every result-relevant file with the tested commits (swarm `3dcf8d6`, engine `398e9f6`, assets `bf629d2`). An **unknown change puts the bot on HOLD**: it alerts you and does **not** submit until you review it. |
| GitHub activity: new or updated pull requests, new tags, new repos | every 10 min | Alerts only. |

## Commands

```bash
git clone https://github.com/FCB-Sunny/sentinel-launch-bot.git ~/sentinel-launch-bot
cd ~/sentinel-launch-bot

bash bot.sh setup                          # one time (a few minutes); must end with READY
bash bot.sh uid    <WALLET> <HOTKEY>       # your UID, protection left, and "no commitment yet: ready"
bash bot.sh check  <WALLET> <HOTKEY>       # uid + guard + rehearsal; must end with "guard OK; ... would submit now"
bash bot.sh start  <WALLET> <HOTKEY>       # the bot runs in the background, LIVE; you can log out
```

Any time:

```bash
bash bot.sh status      # running? guard verdict, submitted?, last log lines
bash bot.sh log         # follow the log live (Ctrl-C leaves the log; the bot keeps running)
bash bot.sh guard       # a GitHub check right now: every repo and branch, with the held files if any
bash bot.sh stop        # stop the bot
bash bot.sh reboot-on   # optional: start it again by itself after a VPS reboot
```

`setup` clones the target repo (`FCB-Sunny/sentinel-v10g`) to `~/sentinel-v10g`, the Swarm code at `3dcf8d6` to
`~/swarm_sentinel`, and creates `~/sentinel_env` with the 64 pinned packages `swarm model verify` and `swarm model submit` need
(no simulator engine, no torch). If `python3.11` is missing: `sudo apt install -y python3.11 python3.11-venv git`
(3.12 also works).

## Your UID

```bash
bash bot.sh uid <WALLET> <HOTKEY>
```

It prints the hotkey's public address, its UID on subnet 124, how much of the 16.7-hour registration
protection is left, and whether the hotkey already holds a commitment. It prints nothing secret. With btcli you can also
run `btcli wallet overview --wallet.name <WALLET> --netuid 124` (btcli 9 spells it `--wallet-name`).

The VPS needs only the hotkey: copy `~/.bittensor/wallets/<WALLET>/coldkeypub.txt` and
`~/.bittensor/wallets/<WALLET>/hotkeys/<HOTKEY>`. Never copy the coldkey. `start` refuses to run if the hotkey is not
registered or already submitted (one submission per hotkey, ever).

## The GitHub guard

Every GitHub change the bot sees is sorted into one of these:

| Kind | Examples | What the bot does |
|---|---|---|
| **tested** | a branch still at the tested commit, or a merge that brings exactly the tested files | nothing |
| **known launch edit** | engine pin `swarm-bullet3==2.0.0.3` -> `2.0.0.5`; swarm-worlds pin moved to a tag whose assets are the tested ones; version lines; registry metadata (task state, display, payout share, other tasks closing) | logs and alerts, **keeps going** |
| **HOLD** | any other change to the Sentinel task code, simulator core, validator, submit code, Docker files, package pins, engine source, the 3D assets, or Sentinel's scoring rules / observation interface in the registry; a PyPI engine newer than 2.0.0.5 that no tested source explains | alerts, **does not submit** |
| info | docs, tests, CI files, lock files, new PRs, tags, repos | logs and alerts |

**When the bot holds:**

1. `bash bot.sh guard` lists the held files.
2. Look at the change on GitHub.
3. If it is harmless, `bash bot.sh accept` (type `ACCEPT`). This accepts exactly those file versions; any later edit to
   them holds again. The running bot picks it up within a minute and submits if Sentinel is open.
4. If it is not harmless, leave the bot on hold (or `bash bot.sh stop`). The model needs re-testing on the new code.

If GitHub cannot be read at launch, the bot decides on its last good check and logs a warning. Unchanged branches are
checked with ETags, which do not count against GitHub's rate limit, so no token is needed.

## Alerts on your phone (optional)

Create `bot.env` in this folder (never committed):

```bash
NOTIFY_DISCORD_WEBHOOK=https://discord.com/api/webhooks/...   # Discord: channel > Edit > Integrations > Webhooks
GITHUB_TOKEN=ghp_...                                          # optional; not needed for the guard
```

With a webhook, the bot posts these events: start, guard changes and holds, GitHub activity, Sentinel live, submitting,
done, stopped.

## At launch, in order

1. The backend shows Sentinel open.
2. A fresh guard check runs (no older than 20 seconds). The verdict must be OK.
3. `swarm model submit` runs once: the official check, the on-chain commit, the upload. If the commit landed but the
   upload did not, only the upload is retried. A restart never commits twice (`bot_state.json`, plus a check of the
   hotkey's commitment on-chain).
4. `DONE: v10g submitted`, and the bot exits. Or `STOPPED` if the backend refused it for good. A crash restarts the bot
   after 20 seconds.

**Run only one submitter.** Don't also submit by hand, and don't run the older `vps/` kits in the sentinel-v10a or
sentinel-v10g repos.

## Files

| File | |
|---|---|
| `bot.sh` | The commands. |
| `sentinel_bot.py` | The bot. |
| `guard.py` | The GitHub guard (`python3 guard.py report` also works on its own). |
| `guard_baseline.json.gz` | Fingerprints of the tested commits' result-relevant files (swarm 309, engine 7,638, assets 3,124). |
| `hotkey_check.py` | UID / protection / commitment check. |
| `setup.sh`, `requirements_submit.txt` | The one-time setup. |
| `target.env` | Which model to submit (name, repo, zip). The expected sha256 is read from the target repo, never stored here. |
