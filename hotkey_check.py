"""Is the Sentinel hotkey ready? Prints its public address, its UID on subnet 124, how long ago it registered (protection
lasts 5000 blocks, ~16.7 h) and whether it already holds a commitment (one submission per hotkey, ever). Reads only the
hotkey's public address; prints nothing secret.
usage: hotkey_check.py WALLET_NAME HOTKEY_NAME   -> exit 0 ready, 1 not ready, 2 could not check
       hotkey_check.py WALLET_NAME HOTKEY_NAME --committed SHA256   -> exit 0 if the hotkey's commitment holds SHA256,
       1 if not, 2 could not check (lets the submitter resume with an upload-only run instead of committing twice)"""
import json, sys

import bittensor as bt

NETUID, IMMUNE_BLOCKS = 124, 5000


def main():
    name, hot = sys.argv[1], sys.argv[2]
    w = bt.Wallet(name=name, hotkey=hot)
    try:
        ss58 = w.hotkeypub.ss58_address
    except Exception:
        try:
            ss58 = w.hotkey.ss58_address
        except Exception as e:
            print(f"NO HOTKEY {name}/{hot} on this machine ({str(e)[:120]}): copy the hotkey file and coldkeypub.txt here")
            return 1
    if "--committed" in sys.argv:
        sha = sys.argv[sys.argv.index("--committed") + 1]
        try:
            c = bt.Subtensor(network="finney").get_all_commitments(netuid=NETUID).get(ss58) or ""
        except Exception as e:
            print(f"could not reach the chain: {str(e)[:160]}")
            return 2
        print(f"commitment of {ss58}: {c or 'none'}")
        return 0 if sha in c else 1
    try:
        st = bt.Subtensor(network="finney")
        blk = st.get_current_block()
        m = st.metagraph(netuid=NETUID, lite=True)
        commits = st.get_all_commitments(netuid=NETUID)
    except Exception as e:
        print(f"could not reach the chain: {str(e)[:160]}")
        return 2
    print(f"hotkey {name}/{hot}: {ss58}")
    if ss58 not in m.hotkeys:
        print("NOT REGISTERED on subnet 124: register it before launch (btcli subnet register --netuid 124 ...)")
        return 1
    uid = m.hotkeys.index(ss58)
    age_blocks = blk - int(m.block_at_registration[uid])
    left_h = max(0, IMMUNE_BLOCKS - age_blocks) * 12 / 3600
    print(f"UID {uid}, registered {age_blocks * 12 / 3600:.1f} h ago, protection left {left_h:.1f} h")
    c = commits.get(ss58)
    if c:
        try:
            d = json.loads(c)
            c = f"family {d.get('f')} sha {str(d.get('s'))[:16]}"
        except Exception:
            pass
        print(f"ALREADY HOLDS A COMMITMENT ({c}): one submission per hotkey, ever - use a new hotkey")
        return 1
    print("no commitment yet: ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
