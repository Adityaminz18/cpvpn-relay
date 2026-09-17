#!/usr/bin/env python3
"""
tunnel.py  -  Cross-platform (Linux + macOS) installer for a persistent
              Check Point SSL VPN tunnel and/or an HAProxy TCP relay.

Chain:
    client --> this host (HAProxy :PORT) --VPN tunnel--> server behind the VPN

Platforms
---------
  Linux : apt + systemd (services + timer)
  macOS : Homebrew + launchd (LaunchDaemons)     [run with sudo]

Config is env-variable driven. Order (first wins):
    CLI flag  >  environment variable  >  --env-file / saved env  >  default
Runtime config lives in <confdir>/cpvpn.env and is read at service start via a
small wrapper, so you can edit it and restart the service to change settings.

Modes
-----
  (default)     set up the VPN tunnel + (optionally) the relay
  --proxy       also set up the HAProxy relay
  --no-vpn      skip the tunnel, set up ONLY the relay (handy for local testing,
                or when the tunnel is provided by the official Check Point client)

macOS note: the pure-Python cpyvpn tunnel is BEST-EFFORT on macOS. The supported
Check Point tunnel on a Mac is the official client (Endpoint Security VPN). If
the tunnel won't come up, use --no-vpn and let the relay forward over whatever
tunnel the official client provides.

Env vars (prefix CPVPN_): GATEWAY, USER, PASSWORD, SUBNET, TEST_IP, VPNC_SCRIPT,
WATCHDOG(true/false), VPN(true/false), PROXY(true/false), PROXY_LISTEN,
PROXY_BACKEND, PROXY_SOURCE, ENV_FILE.

Examples:
    sudo -E python3 tunnel.py --env-file ./cpvpn.env
    sudo python3 tunnel.py --proxy --proxy-backend 10.0.0.5:8080
    sudo CPVPN_VPN=false python3 tunnel.py --proxy   # relay only (test)
    sudo python3 tunnel.py --uninstall
"""

import argparse
import getpass
import os
import platform
import re
import shutil
import subprocess
import sys
import textwrap
import time

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# ---- platform-specific locations -------------------------------------------
def brew_prefix():
    try:
        return subprocess.check_output(["brew", "--prefix"], text=True).strip()
    except Exception:
        return "/opt/homebrew" if platform.machine() == "arm64" else "/usr/local"

if IS_MAC:
    PREFIX = brew_prefix()
    CONF_DIR = f"{PREFIX}/etc/cpvpn"
    LAUNCHD_DIR = "/Library/LaunchDaemons"
    LABEL_VPN = "com.cpvpn.tunnel"
    LABEL_WD = "com.cpvpn.watchdog"
    LABEL_HAP = "com.cpvpn.haproxy"
    HAPROXY_CFG = f"{CONF_DIR}/haproxy.cfg"
else:
    PREFIX = ""
    CONF_DIR = "/etc/cpvpn"
    HAPROXY_CFG = "/etc/haproxy/haproxy.cfg"
    HAPROXY_DROPIN = "/etc/systemd/system/haproxy.service.d/after-cpvpn.conf"
    SERVICE = "cpvpn.service"
    WATCH_SERVICE = "cpvpn-watchdog.service"
    WATCH_TIMER = "cpvpn-watchdog.timer"

ENV_FILE = f"{CONF_DIR}/cpvpn.env"
PW_FILE = f"{CONF_DIR}/passwd.sh"
RUN_TUNNEL = f"{CONF_DIR}/run-tunnel.sh"
WATCH_SCRIPT = f"{CONF_DIR}/watchdog.sh"
LOG_DIR = f"{CONF_DIR}/logs"

DEFAULTS = {
    "CPVPN_GATEWAY": "",
    "CPVPN_USER": "",
    "CPVPN_SUBNET": "",
    "CPVPN_TEST_IP": "",
    "CPVPN_VPNC_SCRIPT": "",
    "CPVPN_WATCHDOG": "true",
    "CPVPN_VPN": "true",
    "CPVPN_PROXY": "false",
    "CPVPN_PROXY_LISTEN": "0.0.0.0:9999",
    "CPVPN_PROXY_BACKEND": "",
    "CPVPN_PROXY_SOURCE": "",
}


def log(m):
    print(f"[cpvpn-setup] {m}", flush=True)


def sh(cmd, check=True):
    log("$ " + (cmd if isinstance(cmd, str) else " ".join(cmd)))
    return subprocess.run(cmd, shell=isinstance(cmd, str), check=check)


def require_root():
    if os.geteuid() != 0:
        sys.exit("Must run as root:  sudo python3 tunnel.py ...")


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def which(name):
    return shutil.which(name)


# ---- env loading -----------------------------------------------------------
def parse_env_file(path):
    out = {}
    try:
        for raw in open(path):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:]
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def preload_env():
    ef = os.environ.get("CPVPN_ENV_FILE")
    if "--env-file" in sys.argv:
        i = sys.argv.index("--env-file")
        if i + 1 < len(sys.argv):
            ef = sys.argv[i + 1]
    for path in [ef, os.path.join(os.getcwd(), ".env"), ENV_FILE]:
        if path and os.path.exists(path):
            for k, v in parse_env_file(path).items():
                os.environ.setdefault(k, v)


def env(key):
    return os.environ.get(key, DEFAULTS.get(key, ""))


# ---- dependency install ----------------------------------------------------
def ensure_brew():
    if not which("brew"):
        sys.exit("Homebrew is required on macOS. Install from https://brew.sh then re-run.")


def install_deps(need_vpn, need_proxy):
    if IS_MAC:
        ensure_brew()
        if need_proxy:
            sh("brew install haproxy", check=False)
        if need_vpn:
            sh("brew install vpnc-scripts", check=False)  # provides the Darwin vpnc-script
    else:
        sh("apt-get update -y")
        pkgs = ["python3-pip"]
        if need_vpn:
            pkgs.append("vpnc-scripts")
        if need_proxy:
            pkgs.append("haproxy")
        sh("apt-get install -y " + " ".join(pkgs))
    if need_vpn:
        log("Installing cpyvpn (pip) ...")
        sh(f"{sys.executable} -m pip install --upgrade cpyvpn --break-system-packages",
           check=False)


def find_cp_client():
    p = which("cp_client")
    if p:
        return p
    # common fallbacks
    for c in ("/usr/local/bin/cp_client", f"{PREFIX}/bin/cp_client" if PREFIX else ""):
        if c and os.path.exists(c):
            return c
    sys.exit("cp_client not found after install - check the pip output above.")


def find_vpnc_script(override):
    if override:
        if os.path.exists(override):
            return override
        sys.exit(f"CPVPN_VPNC_SCRIPT={override} does not exist.")
    cands = ["/usr/share/vpnc-scripts/vpnc-script", "/etc/vpnc/vpnc-script"]
    if PREFIX:
        cands += [f"{PREFIX}/etc/vpnc/vpnc-script",
                  f"{PREFIX}/share/vpnc-scripts/vpnc-script",
                  f"{PREFIX}/libexec/vpnc-script"]
    for p in cands:
        if os.path.exists(p):
            return p
    # ask package managers
    try:
        if IS_LINUX:
            out = subprocess.check_output("dpkg -L vpnc-scripts | grep -E 'vpnc-script$'",
                                          shell=True, text=True)
        else:
            out = subprocess.check_output("brew list vpnc-scripts 2>/dev/null | grep -E 'vpnc-script$'",
                                          shell=True, text=True)
        for line in out.splitlines():
            if os.path.exists(line.strip()):
                return line.strip()
    except subprocess.CalledProcessError:
        pass
    sys.exit("Could not locate a vpnc-script. Install 'vpnc-scripts' (Linux) or "
             "'brew install vpnc-scripts' (macOS), or set CPVPN_VPNC_SCRIPT.")


# ---- patch cpyvpn (Linux/macOS both use the vpnc VNA path) -----------------
PATCHED_METHOD = (
    "    def run_vpnc(self, reason):\n"
    "        # cpvpn-patched: put VPN vars in env (not kw) and guard None/unset values\n"
    "        env = dict(self._env)\n"
    '        env.update({"reason": reason})\n'
    "        try:\n"
    "            _omip = self.om_ip()\n"
    "        except Exception:\n"
    "            _omip = None\n"
    "        if _omip:\n"
    '            env["INTERNAL_IP4_ADDRESS"] = _omip\n'
    '        if getattr(self, "netmask", None):\n'
    '            env["INTERNAL_IP4_NETMASK"] = self.netmask\n'
    '        if getattr(self, "gw", None):\n'
    '            env["VPNGATEWAY"] = self.gw\n'
    '        kw = {"env": env, "stdin": DEVNULL, "stdout": DEVNULL, "stderr": STDOUT, "check": True}\n'
    "        subprocess.run([self._vpnc], **kw)\n"
    "\n"
)


def patch_cpyvpn():
    try:
        import cpyvpn  # noqa
    except ImportError:
        sys.exit("cpyvpn not importable after install - check pip output above.")
    vna = os.path.join(os.path.dirname(cpyvpn.__file__), "vna.py")
    src = open(vna).read()
    if "cpvpn-patched" in src:
        log(f"cpyvpn already patched ({vna}).")
        return
    pat = re.compile(r"    def run_vpnc\(self, reason\):.*?(?=^    def )",
                     re.DOTALL | re.MULTILINE)
    new, n = pat.subn(PATCHED_METHOD, src, count=1)
    if n != 1:
        log("WARNING: could not patch cpyvpn run_vpnc() - layout differs. "
            "Tunnel may fail; tell me and I'll adjust.")
        return
    open(vna + ".orig", "w").write(src)
    open(vna, "w").write(new)
    log(f"Patched {vna} (backup at {vna}.orig).")


# ---- config files ----------------------------------------------------------
def shell_quote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


def write_common(cfg, password):
    os.makedirs(CONF_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    # password helper
    with open(PW_FILE, "w") as f:
        f.write("#!/bin/sh\nprintf '%s' " + shell_quote(password) + "\n")
    os.chmod(PW_FILE, 0o700)
    # env file
    lines = ["# cpvpn runtime config - edit then restart the service\n"]
    for k in ("CPVPN_GATEWAY", "CPVPN_USER", "CPVPN_SUBNET", "CPVPN_TEST_IP",
              "CPVPN_VPNC_SCRIPT", "CPVPN_PASSWD_SCRIPT", "CPVPN_WATCHDOG",
              "CPVPN_VPN", "CPVPN_PROXY", "CPVPN_PROXY_LISTEN",
              "CPVPN_PROXY_BACKEND", "CPVPN_PROXY_SOURCE", "CPVPN_CP_CLIENT"):
        lines.append(f"{k}={cfg.get(k, '')}\n")
    open(ENV_FILE, "w").write("".join(lines))
    os.chmod(ENV_FILE, 0o600)
    if os.geteuid() == 0:
        for f in (PW_FILE, ENV_FILE):
            os.chown(f, 0, 0)


def write_tunnel_wrapper():
    open(RUN_TUNNEL, "w").write(textwrap.dedent(f"""\
        #!/bin/sh
        . {ENV_FILE}
        exec "$CPVPN_CP_CLIENT" -m l -u "$CPVPN_USER" \\
            --passwd-script "$CPVPN_PASSWD_SCRIPT" \\
            -s "$CPVPN_VPNC_SCRIPT" "$CPVPN_GATEWAY"
        """))
    os.chmod(RUN_TUNNEL, 0o755)


def restart_cmd():
    return (f"systemctl restart {SERVICE}" if IS_LINUX
            else f"launchctl kickstart -k system/{LABEL_VPN}")


def write_watchdog_script():
    open(WATCH_SCRIPT, "w").write(textwrap.dedent(f"""\
        #!/bin/sh
        . {ENV_FILE} 2>/dev/null
        TARGET="$CPVPN_TEST_IP"
        [ -n "$TARGET" ] || exit 0
        if ! ping -c2 -W3 "$TARGET" >/dev/null 2>&1; then
            logger -t cpvpn-watchdog "ping $TARGET failed - restarting tunnel" 2>/dev/null || true
            {restart_cmd()}
        fi
        """))
    os.chmod(WATCH_SCRIPT, 0o755)


# ---- HAProxy config (same on both platforms) -------------------------------
def write_haproxy(listen, backend, source):
    os.makedirs(os.path.dirname(HAPROXY_CFG), exist_ok=True)
    if os.path.exists(HAPROXY_CFG) and not os.path.exists(HAPROXY_CFG + ".pre-cpvpn"):
        shutil.copy(HAPROXY_CFG, HAPROXY_CFG + ".pre-cpvpn")
    acl = f"    tcp-request connection reject unless {{ src {source} }}\n" if source else ""
    userline = "" if IS_MAC else "    user haproxy\n    group haproxy\n"
    cfg = (
        "global\n"
        "    maxconn 2000\n"
        f"{userline}"
        "\n"
        "defaults\n"
        "    mode    tcp\n"
        "    option  tcplog\n"
        "    option  dontlognull\n"
        "    timeout connect 10s\n"
        "    timeout client  1m\n"
        "    timeout server  1m\n"
        "    retries 3\n"
        "\n"
        "frontend relay_in\n"
        f"    bind {listen}\n"
        f"{acl}"
        "    default_backend relay_out\n"
        "\n"
        "backend relay_out\n"
        "    option tcp-check\n"
        f"    server target {backend} check inter 10s fall 3 rise 2\n"
    )
    open(HAPROXY_CFG, "w").write(cfg)
    hap = which("haproxy") or "haproxy"
    sh(f"{hap} -c -f {HAPROXY_CFG}")
    log(f"Wrote {HAPROXY_CFG} (relay {listen} -> {backend}"
        + (f", only from {source}" if source else "") + ").")


# ---- service layer: Linux (systemd) ----------------------------------------
def linux_install_services(watchdog, proxy):
    unit = textwrap.dedent(f"""\
        [Unit]
        Description=Check Point VPN tunnel (cpyvpn)
        After=network-online.target
        Wants=network-online.target
        StartLimitIntervalSec=0

        [Service]
        Type=simple
        ExecStart={RUN_TUNNEL}
        Restart=always
        RestartSec=5

        [Install]
        WantedBy=multi-user.target
        """)
    open(f"/etc/systemd/system/{SERVICE}", "w").write(unit)
    if watchdog:
        open(f"/etc/systemd/system/{WATCH_SERVICE}", "w").write(textwrap.dedent(f"""\
            [Unit]
            Description=cpvpn watchdog
            After={SERVICE}
            [Service]
            Type=oneshot
            ExecStart={WATCH_SCRIPT}
            """))
        open(f"/etc/systemd/system/{WATCH_TIMER}", "w").write(textwrap.dedent(f"""\
            [Unit]
            Description=Run cpvpn watchdog every minute
            [Timer]
            OnBootSec=2min
            OnUnitActiveSec=60s
            AccuracySec=10s
            [Install]
            WantedBy=timers.target
            """))
    if proxy:
        os.makedirs(os.path.dirname(HAPROXY_DROPIN), exist_ok=True)
        open(HAPROXY_DROPIN, "w").write(f"[Unit]\nAfter={SERVICE}\nWants={SERVICE}\n")


def linux_enable(vpn, watchdog, proxy):
    sh("systemctl daemon-reload")
    if vpn:
        sh(f"systemctl enable --now {SERVICE}")
        if watchdog:
            sh(f"systemctl enable --now {WATCH_TIMER}")
    if proxy:
        sh("systemctl enable --now haproxy")
        sh("systemctl restart haproxy")


# ---- service layer: macOS (launchd) ----------------------------------------
def plist(label, args, keepalive=True, run_at_load=True, start_interval=None):
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0"><dict>',
        f'  <key>Label</key><string>{label}</string>',
        '  <key>ProgramArguments</key><array>',
    ]
    for a in args:
        body.append(f'    <string>{a}</string>')
    body.append('  </array>')
    if run_at_load:
        body.append('  <key>RunAtLoad</key><true/>')
    if keepalive:
        body.append('  <key>KeepAlive</key><true/>')
    if start_interval:
        body.append(f'  <key>StartInterval</key><integer>{start_interval}</integer>')
    body.append(f'  <key>StandardOutPath</key><string>{LOG_DIR}/{label}.out.log</string>')
    body.append(f'  <key>StandardErrorPath</key><string>{LOG_DIR}/{label}.err.log</string>')
    body.append('</dict></plist>')
    return "\n".join(body) + "\n"


def mac_write_plist(label, args, **kw):
    path = f"{LAUNCHD_DIR}/{label}.plist"
    open(path, "w").write(plist(label, args, **kw))
    os.chown(path, 0, 0)
    os.chmod(path, 0o644)
    return path


def mac_install_services(vpn, watchdog, proxy):
    paths = []
    if vpn:
        paths.append(mac_write_plist(LABEL_VPN, [RUN_TUNNEL], keepalive=True))
        if watchdog:
            paths.append(mac_write_plist(LABEL_WD, [WATCH_SCRIPT],
                                         keepalive=False, start_interval=60))
    if proxy:
        hap = which("haproxy") or f"{PREFIX}/bin/haproxy"
        paths.append(mac_write_plist(LABEL_HAP, [hap, "-f", HAPROXY_CFG, "-db"],
                                     keepalive=True))
    return paths


def mac_enable(paths):
    for p in paths:
        sh(f"launchctl unload {p}", check=False)
        sh(f"launchctl load -w {p}")


# ---- verify ----------------------------------------------------------------
def verify(test_ip, subnet, proxy_listen, vpn):
    if vpn:
        log("Waiting for tunnel to establish ...")
        time.sleep(8)
        if IS_LINUX:
            sh(f"systemctl --no-pager --full status {SERVICE}", check=False)
        else:
            sh(f"launchctl print system/{LABEL_VPN} | head -20", check=False)
        sh("ifconfig 2>/dev/null | grep -i -A1 'utun\\|tun' | head || "
           "ip -brief addr | grep -i tun || true", check=False)
    if test_ip:
        log(f"Pinging {test_ip} ...")
        r = subprocess.run(f"ping -c3 {test_ip}", shell=True)
        log("SUCCESS: reachable." if r.returncode == 0 else
            f"{test_ip} did not answer (expected if --no-vpn and no other tunnel).")
    if proxy_listen:
        port = proxy_listen.rsplit(":", 1)[-1]
        if IS_MAC:
            sh(f"lsof -nP -iTCP:{port} -sTCP:LISTEN || echo '  (relay not listening?)'",
               check=False)
        else:
            sh(f"ss -lntp | grep {port} || echo '  (relay not listening?)'", check=False)


# ---- uninstall -------------------------------------------------------------
def uninstall():
    if IS_LINUX:
        for u in (WATCH_TIMER, WATCH_SERVICE, SERVICE):
            sh(f"systemctl disable --now {u}", check=False)
            p = f"/etc/systemd/system/{u}"
            if os.path.exists(p):
                os.remove(p)
        if os.path.exists(HAPROXY_DROPIN):
            os.remove(HAPROXY_DROPIN)
        if os.path.exists(HAPROXY_CFG + ".pre-cpvpn"):
            shutil.move(HAPROXY_CFG + ".pre-cpvpn", HAPROXY_CFG)
            sh("systemctl restart haproxy", check=False)
        else:
            sh("systemctl disable --now haproxy", check=False)
        sh("systemctl daemon-reload", check=False)
    else:
        for lbl in (LABEL_WD, LABEL_HAP, LABEL_VPN):
            p = f"{LAUNCHD_DIR}/{lbl}.plist"
            sh(f"launchctl unload {p}", check=False)
            if os.path.exists(p):
                os.remove(p)
    if os.path.isdir(CONF_DIR):
        shutil.rmtree(CONF_DIR, ignore_errors=True)
    log("Uninstalled cpvpn services, relay, and config.")


# ---- main ------------------------------------------------------------------
def main():
    preload_env()
    ap = argparse.ArgumentParser(description="Cross-platform Check Point VPN tunnel + HAProxy relay.")
    ap.add_argument("--env-file")
    ap.add_argument("--gateway", default=env("CPVPN_GATEWAY"))
    ap.add_argument("--user", default=env("CPVPN_USER"))
    ap.add_argument("--password", default=os.environ.get("CPVPN_PASSWORD"))
    ap.add_argument("--subnet", default=env("CPVPN_SUBNET"))
    ap.add_argument("--test-ip", default=env("CPVPN_TEST_IP"))
    ap.add_argument("--vpnc-script", default=env("CPVPN_VPNC_SCRIPT"))
    ap.add_argument("--no-vpn", action="store_true", help="skip the tunnel; relay only")
    ap.add_argument("--no-watchdog", action="store_true")
    ap.add_argument("--proxy", action="store_true")
    ap.add_argument("--proxy-listen", default=env("CPVPN_PROXY_LISTEN"))
    ap.add_argument("--proxy-backend", default=env("CPVPN_PROXY_BACKEND"))
    ap.add_argument("--proxy-source", default=env("CPVPN_PROXY_SOURCE") or None)
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

    if not (IS_LINUX or IS_MAC):
        sys.exit(f"Unsupported platform: {sys.platform} (Linux and macOS only).")
    require_root()

    if args.uninstall:
        uninstall()
        return

    vpn = not args.no_vpn and truthy(env("CPVPN_VPN"))
    watchdog = vpn and not args.no_watchdog and truthy(env("CPVPN_WATCHDOG"))
    proxy = args.proxy or truthy(env("CPVPN_PROXY"))
    if not vpn and not proxy:
        sys.exit("Nothing to do: VPN disabled and --proxy not set.")

    log(f"Platform: {'macOS' if IS_MAC else 'Linux'}  |  VPN={vpn}  watchdog={watchdog}  proxy={proxy}")

    if proxy and not args.proxy_backend:
        sys.exit("--proxy needs --proxy-backend HOST:PORT (or CPVPN_PROXY_BACKEND).")
    if watchdog and not args.test_ip:
        sys.exit("The watchdog needs --test-ip (an IP behind the VPN that answers ping), "
                 "or pass --no-watchdog.")
    if vpn and not (args.gateway and args.user):
        sys.exit("VPN mode needs --gateway and --user (or CPVPN_GATEWAY / CPVPN_USER). "
                 "Use --no-vpn for relay only.")

    password = ""
    if vpn:
        password = args.password or getpass.getpass(f"VPN password for {args.user}: ")
        if not password:
            sys.exit("No password (set CPVPN_PASSWORD, --password, or type it).")

    install_deps(need_vpn=vpn, need_proxy=proxy)

    cp_client = ""
    vpnc = ""
    if vpn:
        patch_cpyvpn()
        cp_client = find_cp_client()
        vpnc = find_vpnc_script(args.vpnc_script)
        log(f"cp_client: {cp_client}   vpnc-script: {vpnc}")

    cfg = {
        "CPVPN_GATEWAY": args.gateway, "CPVPN_USER": args.user,
        "CPVPN_SUBNET": args.subnet, "CPVPN_TEST_IP": args.test_ip,
        "CPVPN_VPNC_SCRIPT": vpnc, "CPVPN_PASSWD_SCRIPT": PW_FILE,
        "CPVPN_CP_CLIENT": cp_client,
        "CPVPN_WATCHDOG": "true" if watchdog else "false",
        "CPVPN_VPN": "true" if vpn else "false",
        "CPVPN_PROXY": "true" if proxy else "false",
        "CPVPN_PROXY_LISTEN": args.proxy_listen,
        "CPVPN_PROXY_BACKEND": args.proxy_backend,
        "CPVPN_PROXY_SOURCE": args.proxy_source or "",
    }

    write_common(cfg, password or "unused")
    if vpn:
        write_tunnel_wrapper()
        if watchdog:
            write_watchdog_script()
    if proxy:
        write_haproxy(args.proxy_listen, args.proxy_backend, args.proxy_source)

    if IS_LINUX:
        linux_install_services(watchdog, proxy)
        linux_enable(vpn, watchdog, proxy)
    else:
        paths = mac_install_services(vpn, watchdog, proxy)
        mac_enable(paths)

    verify(args.test_ip, args.subnet, args.proxy_listen if proxy else None, vpn)

    log(f"Done. Config: {ENV_FILE}")
    if IS_LINUX:
        log(f"  systemctl status {SERVICE}   |   journalctl -u {SERVICE} -f")
    else:
        log(f"  sudo launchctl print system/{LABEL_VPN}")
        log(f"  logs in {LOG_DIR}/")
    log(f"  sudo python3 {os.path.basename(__file__)} --uninstall")


if __name__ == "__main__":
    main()