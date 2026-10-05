#!/usr/bin/env python3
"""
checkcreds.py — probe Check Point VPN credentials without bringing the tunnel up.

Reads CPVPN_GATEWAY / CPVPN_USER from .env, prompts for the password, and runs
cp_client just long enough to see whether auth succeeds.

Heuristic (not a protocol parse): bad creds / unreachable gateway → cp_client
exits non-zero within a few seconds; good creds → it keeps running to set up
the tunnel, so we hit the timeout and call it OK.

Usage:
    python3 checkcreds.py                   # prompts for password
    CPVPN_PASSWORD=... python3 checkcreds.py
"""
import os, sys, shlex, shutil, getpass, tempfile, subprocess


def env_from_dotenv(path=".env"):
    out = {}
    try:
        for raw in open(path):
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def main():
    if os.geteuid() != 0:
        sys.exit("needs root (cpyvpn opens a utun device before auth):  "
                 "sudo -E python3 checkcreds.py")
    e = env_from_dotenv()
    gw = e.get("CPVPN_GATEWAY") or sys.exit("CPVPN_GATEWAY not set in .env")
    user = e.get("CPVPN_USER") or sys.exit("CPVPN_USER not set in .env")
    pw = os.environ.get("CPVPN_PASSWORD") or getpass.getpass(f"VPN password for {user}@{gw}: ")
    if not pw:
        sys.exit("no password given")

    cp = shutil.which("cp_client") or sys.exit(
        "cp_client not on PATH — install cpyvpn:  pip install cpyvpn")

    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(f"#!/bin/sh\nprintf %s {shlex.quote(pw)}\n")
        pwscript = f.name
    os.chmod(pwscript, 0o700)

    print(f"→ probing {gw} as {user} (15s)...\n")
    try:
        # ponytail: time-box the call. cp_client exits fast on auth failure;
        # on success it keeps running trying to open the tunnel.
        r = subprocess.run(
            # -s /usr/bin/true: cp_client requires a vpnc-script, but we don't
            # want it to touch routes. /usr/bin/true is a safe no-op.
            [cp, "-m", "l", "-u", user, "--passwd-script", pwscript,
             "-s", "/usr/bin/true", gw],
            capture_output=True, text=True, timeout=15,
        )
        print(r.stdout + r.stderr)
        print("❌ FAILED — cp_client exited (rc=%d). Likely bad creds or "
              "unreachable gateway." % r.returncode)
        sys.exit(1)
    except subprocess.TimeoutExpired as e:
        def _s(x): return x.decode(errors="replace") if isinstance(x, bytes) else (x or "")
        out = _s(e.stdout) + _s(e.stderr)
        print(out)
        # if the output already says "authentication failed", trust that
        if any(w in out.lower() for w in ("auth fail", "access denied", "wrong", "invalid user")):
            print("❌ FAILED — gateway rejected the credentials.")
            sys.exit(1)
        print("✅ OK — cp_client is past auth and setting up the tunnel.")
        sys.exit(0)
    finally:
        try: os.remove(pwscript)
        except OSError: pass


if __name__ == "__main__":
    main()
