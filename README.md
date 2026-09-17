# cpvpn-relay

cpvpn-relay lets **any device, browser or app** reach a **TCP service that is only reachable through a Check Point SSL VPN** — a web app, an API, a database, an attendance server, anything that speaks TCP.
One relay host keeps the VPN connected and forwards traffic to that service. The clients themselves need no VPN client, and nothing has to be installed on them.

```
  any client (device/phone/PC)      Relay host                          Remote network
                                    (Ubuntu server or Mac)
 ┌────────────────────────┐       ┌────────────────────────┐          ┌──────────────────┐
 │ <relay-ip>:9999        │ ────► │ HAProxy :9999          │ ─ VPN ─► │ target service   │
 └────────────────────────┘  LAN  │ + Check Point VPN      │          │ 192.168.0.152:80 │
                                  └────────────────────────┘          └──────────────────┘
```

**Tested on Ubuntu Server (main platform) and macOS**, relaying an eSSL attendance server's web app.

> The IPs in this README (`192.168.0.152`, `192.168.0.0/24`, ...) are examples. Replace them with your own.

---

## Contents

1. [How it works](#how-it-works)
2. [Quick start (Ubuntu)](#quick-start-ubuntu)
3. [Files](#files)
4. [Requirements](#requirements)
5. [Setup on Ubuntu](#setup-on-ubuntu)
6. [Setup on macOS](#setup-on-macos)
7. [Using the relay](#using-the-relay)
8. [Managing the services](#managing-the-services)
9. [Configuration](#configuration)
10. [Checking that everything works](#checking-that-everything-works)
11. [macOS: internal Wi-Fi subnet overlap (`lanfix.py`)](#macos-internal-wi-fi-subnet-overlap-lanfixpy)
12. [Troubleshooting](#troubleshooting)
13. [Uninstall](#uninstall)
14. [Security notes](#security-notes)

---

## How it works

`tunnel.py` is a one-time **installer**. It sets everything up as background services and then exits. The services keep running and start again after a reboot.

| | **Ubuntu** (main platform) | **macOS** |
|---|---|---|
| VPN tunnel | Built in: the [`cpyvpn`](https://pypi.org/project/cpyvpn/) Check Point client, run as the `cpvpn` systemd service | The official **Check Point Endpoint Security VPN** app (use `--no-vpn`) |
| Tunnel watchdog | `cpvpn-watchdog.timer` pings the test IP every minute and restarts the tunnel if the ping fails | Handled by the VPN app |
| Relay | HAProxy (installed with apt), `haproxy` systemd service | HAProxy (installed with Homebrew), `com.cpvpn.haproxy` launchd service |
| Config directory | `/etc/cpvpn/` | `/opt/homebrew/etc/cpvpn/` |
| Installs dependencies | Yes (apt + pip) | HAProxy must be installed first with `brew` |

**HAProxy** listens on port `9999`, checks every 10 s that the server is up, and forwards TCP connections unchanged through the VPN to the target service.

---

## Quick start (Ubuntu)

```sh
git clone <repo-url> cpvpn-relay && cd cpvpn-relay
cp .env.example .env
nano .env        # set CPVPN_GATEWAY, CPVPN_USER, CPVPN_PROXY_BACKEND, CPVPN_TEST_IP
sudo python3 tunnel.py --proxy      # asks for the VPN password
```

Then point clients at `http://<ubuntu-server-ip>:9999/<app-path>`.

---

## Files

| File | What it does |
|---|---|
| `tunnel.py` | Installer for the VPN tunnel and the HAProxy relay (Ubuntu and macOS). Also handles `--uninstall`. |
| `.env.example` | Sample settings. Copy it to `.env` (ignored by git) and edit. |
| `lanfix.py` | **macOS only.** Fixes a routing clash when the Mac's own Wi-Fi uses the same subnet as the office network. See [the lanfix section](#macos-internal-wi-fi-subnet-overlap-lanfixpy). |

### What the installer creates

**Ubuntu**

| Path | Contents |
|---|---|
| `/etc/cpvpn/cpvpn.env` | Saved settings (root only). The tunnel reads them each time it starts. |
| `/etc/cpvpn/passwd.sh` | Script that gives the VPN password to the client (root only) |
| `/etc/cpvpn/run-tunnel.sh` | Starts `cp_client` using the saved settings |
| `/etc/cpvpn/watchdog.sh` | Pings the test IP and restarts the tunnel if it fails |
| `/etc/systemd/system/cpvpn.service` | VPN tunnel service (restarts itself 5 s after it stops) |
| `/etc/systemd/system/cpvpn-watchdog.{service,timer}` | Watchdog: first run 2 min after boot, then every 60 s |
| `/etc/haproxy/haproxy.cfg` | Relay config. Any original file is backed up to `haproxy.cfg.pre-cpvpn`. |
| `/etc/systemd/system/haproxy.service.d/after-cpvpn.conf` | Makes HAProxy start after the tunnel |

It also installs `haproxy`, `vpnc-scripts` and `python3-pip` with apt, and `cpyvpn` with pip. It then makes a small fix to `cpyvpn` (`vna.py`, keeping the original as `vna.py.orig`) so that it hands its network settings to `vpnc-script` correctly.

**macOS**

| Path | Contents |
|---|---|
| `/opt/homebrew/etc/cpvpn/haproxy.cfg` | Relay config |
| `/opt/homebrew/etc/cpvpn/cpvpn.env` | Saved settings |
| `/opt/homebrew/etc/cpvpn/logs/` | Service logs (`com.cpvpn.haproxy.out.log` / `.err.log`) |
| `/Library/LaunchDaemons/com.cpvpn.haproxy.plist` | Relay service |

---

## Requirements

**Ubuntu**
- Ubuntu Server with systemd and apt
- Python 3 and `sudo`
- A Check Point VPN account: gateway address, username and password
- Outbound HTTPS (TCP 443) from the server to the VPN gateway
- An IP in the office network that **answers ping**, for the setup check and the watchdog

**macOS**
- [Homebrew](https://brew.sh) and Python 3
- **Check Point Endpoint Security VPN** app, installed and able to connect
- An admin account (`sudo`)

---

## Setup on Ubuntu

### 1. Get the code and configure

```sh
git clone <repo-url> cpvpn-relay
cd cpvpn-relay
cp .env.example .env
nano .env
```

At minimum, set these values:

```ini
CPVPN_GATEWAY=vpn.example.com
CPVPN_USER=your-vpn-user
CPVPN_PROXY_BACKEND=192.168.0.152:80
CPVPN_TEST_IP=192.168.0.152
```

Leave `CPVPN_PASSWORD` empty to be asked for it during setup. That's safer than keeping it in a file.

### 2. Run the installer

```sh
sudo python3 tunnel.py --proxy
```

Or, without a `.env` file:

```sh
sudo python3 tunnel.py --proxy \
    --gateway vpn.example.com --user your-vpn-user \
    --proxy-backend 192.168.0.152:80 --test-ip 192.168.0.152
```

What happens:

1. Runs `apt-get install haproxy vpnc-scripts python3-pip` and `pip install cpyvpn`
2. Applies the small `cpyvpn` fix
3. Writes the config, the password helper, the tunnel wrapper and the watchdog to `/etc/cpvpn/`
4. Writes `/etc/haproxy/haproxy.cfg` and checks it with `haproxy -c`
5. Enables and starts `cpvpn.service`, `cpvpn-watchdog.timer` and `haproxy`
6. Waits 8 s, then shows the tunnel status, pings the test IP and checks that port 9999 is listening

A successful run ends with:

```
[cpvpn-setup] SUCCESS: reachable.
LISTEN 0 ... 0.0.0.0:9999 ... users:(("haproxy",...))
[cpvpn-setup] Done. Config: /etc/cpvpn/cpvpn.env
```

### 3. Open the firewall (if `ufw` is active)

```sh
sudo ufw allow 9999/tcp
# or allow only your client network:
sudo ufw allow from 192.168.1.0/24 to any port 9999 proto tcp
```

On a cloud VM, also allow TCP `9999` in the provider's firewall (security group).

---

## Setup on macOS

On macOS the official Check Point app provides the tunnel. `tunnel.py` only installs the relay.

### 1. Install HAProxy (as your normal user, **not** with sudo)

```sh
brew install haproxy
```

> Homebrew refuses to run as root, so `tunnel.py` can't install HAProxy for you.
> It still tries, and prints an error you can ignore.

### 2. Connect the VPN app and check the server

```sh
ping -c3 192.168.0.152
```

### 3. Install the relay

```sh
cd cpvpn-relay
sudo python3 tunnel.py --no-vpn --proxy --proxy-backend 192.168.0.152:80 --test-ip 192.168.0.152
```

> If your `.env` has `CPVPN_VPN=true` (the default in `.env.example`), keep the `--no-vpn` flag or set `CPVPN_VPN=false`.

### 4. Allow HAProxy through the macOS firewall

```sh
HAP=$(realpath /opt/homebrew/bin/haproxy)
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add "$HAP" --unblockapp "$HAP"
```

> Repeat this after `brew upgrade haproxy`, because the binary's path changes with each version.

`tunnel.py` also has a built-in VPN mode on macOS (without `--no-vpn`), but it's best-effort. The official app is the supported option there.

---

## Using the relay

### 1. Find the relay host's IP

| Platform | Command |
|---|---|
| Ubuntu | `hostname -I` |
| macOS | `ipconfig getifaddr en0` |

### 2. Point clients at the relay

Keep the path and change only the host and port:

```
Direct (on the office network):  http://192.168.0.152/<app-path>
Through the relay:               http://<relay-ip>:9999/<app-path>
```

For appliances such as attendance or CCTV devices, set the server address to `<relay-ip>` and the port to `9999` in the device's own server settings.
For non-HTTP services (database, SSH, custom protocols) point the client at `<relay-ip>:9999` the same way — the relay forwards raw TCP and doesn't care about the protocol.

> **A "404 - File or directory not found" page means the relay is working.**
> In the example setup the target is an IIS web app with nothing at `/`, so the app path is required.

### Where clients can be

- **On the same network as the relay host.** Clients connect to the relay's LAN IP.
- **On the relay host itself.** No relay is needed. Go directly to `http://192.168.0.152/...`, because the host is on the VPN.
- **macOS relay on a Wi-Fi that also uses the office subnet** (e.g. both `192.168.0.x`): run `lanfix.py` first. See [the lanfix section](#macos-internal-wi-fi-subnet-overlap-lanfixpy).

---

## Managing the services

### Ubuntu

| Task | Command |
|---|---|
| Tunnel status | `systemctl status cpvpn` |
| Tunnel logs (live) | `journalctl -u cpvpn -f` |
| Watchdog runs | `systemctl list-timers cpvpn-watchdog.timer` |
| Watchdog restarts | `journalctl -t cpvpn-watchdog` |
| Relay status / logs | `systemctl status haproxy` / `journalctl -u haproxy -f` |
| Restart tunnel | `sudo systemctl restart cpvpn` |
| Restart relay | `sudo systemctl restart haproxy` |
| **Stop everything** | `sudo systemctl stop cpvpn-watchdog.timer cpvpn haproxy` |
| **Start everything** | `sudo systemctl start cpvpn haproxy cpvpn-watchdog.timer` |
| Don't start at boot | `sudo systemctl disable --now cpvpn-watchdog.timer cpvpn haproxy` |
| Start at boot again | `sudo systemctl enable --now cpvpn haproxy cpvpn-watchdog.timer` |

> Stop the **watchdog timer first**. Otherwise its next ping fails and it starts the tunnel again.

### macOS

| Task | Command |
|---|---|
| Is it running? | `pgrep -lf haproxy` |
| Is it listening? | `sudo lsof -nP -iTCP:9999 -sTCP:LISTEN` |
| Service details | `sudo launchctl print system/com.cpvpn.haproxy` |
| Restart | `sudo launchctl kickstart -k system/com.cpvpn.haproxy` |
| **Stop** (starts again at next reboot) | `sudo launchctl unload /Library/LaunchDaemons/com.cpvpn.haproxy.plist` |
| **Stop and keep it off** | `sudo launchctl unload -w /Library/LaunchDaemons/com.cpvpn.haproxy.plist` |
| **Start** | `sudo launchctl load -w /Library/LaunchDaemons/com.cpvpn.haproxy.plist` |
| Logs | `tail -f /opt/homebrew/etc/cpvpn/logs/com.cpvpn.haproxy.err.log` |

> `lsof` without `sudo` shows nothing, because HAProxy runs as root. That doesn't mean it's stopped.

---

## Configuration

### All options

| Flag | Env variable | Example / default | Meaning |
|---|---|---|---|
| `--gateway` | `CPVPN_GATEWAY` | `vpn.example.com` | Check Point VPN gateway (built-in VPN mode) |
| `--user` | `CPVPN_USER` | `your-vpn-user` | VPN username |
| `--password` | `CPVPN_PASSWORD` | *(asked if empty)* | VPN password |
| `--subnet` | `CPVPN_SUBNET` | `192.168.0.0/24` | Office subnet (saved in the config for reference) |
| `--test-ip` | `CPVPN_TEST_IP` | `192.168.0.152` | Pinged after setup and by the watchdog. **Must answer ping.** |
| `--vpnc-script` | `CPVPN_VPNC_SCRIPT` | *(auto-detected)* | Path to `vpnc-script`, if auto-detection fails |
| `--no-vpn` | `CPVPN_VPN=false` | VPN on | Skip the built-in tunnel, install the relay only |
| `--no-watchdog` | `CPVPN_WATCHDOG=false` | watchdog on | Don't install the watchdog |
| `--proxy` | `CPVPN_PROXY=true` | relay off | Install the HAProxy relay |
| `--proxy-listen` | `CPVPN_PROXY_LISTEN` | `0.0.0.0:9999` | Address and port the relay listens on |
| `--proxy-backend` | `CPVPN_PROXY_BACKEND` | `192.168.0.152:80` | **Required.** Target service `ip:port` behind the VPN |
| `--proxy-source` | `CPVPN_PROXY_SOURCE` | *(empty = allow all)* | Only accept clients from this IP or subnet |
| `--env-file` | `CPVPN_ENV_FILE` | — | Read settings from this file |
| `--uninstall` | — | — | Remove services, relay config and saved settings |

### Where settings come from (first match wins)

1. Command-line flag
2. Environment variable (e.g. `sudo CPVPN_PROXY_BACKEND=... python3 tunnel.py`)
3. `--env-file <file>`, then `./.env` (current folder), then the **saved** config (`/etc/cpvpn/cpvpn.env` or `/opt/homebrew/etc/cpvpn/cpvpn.env`)
4. Defaults in `tunnel.py`

**Values you pass are saved and reused on the next run.** To clear a saved value, such as `--proxy-source`, edit the saved `cpvpn.env` with `sudo`, or uninstall and set up again.

### Changing settings

**Relay changes:** re-run the installer with the new values. It rewrites the HAProxy config and restarts HAProxy.

```sh
sudo python3 tunnel.py --proxy --proxy-backend 192.168.0.152:9999     # different server port
sudo python3 tunnel.py --proxy --proxy-listen 0.0.0.0:8080            # different relay port
sudo python3 tunnel.py --proxy --proxy-source 192.168.1.0/24          # restrict clients
```

On Ubuntu each re-run asks for the VPN password again, because the password isn't saved in `cpvpn.env`. On macOS, add `--no-vpn` to each command.

**VPN changes (Ubuntu):** a re-run does **not** restart an already running tunnel. After changing VPN settings, restart it yourself:

- **Gateway or username:** edit `/etc/cpvpn/cpvpn.env` (no re-run needed), then `sudo systemctl restart cpvpn`. The tunnel reads this file every time it starts.
- **Password:** re-run the installer, which rewrites `passwd.sh`, then `sudo systemctl restart cpvpn`.

### Example server ports

One relay forwards **one port**. In the example setup the target server accepts two:

| Port | Service |
|---|---|
| `80` | IIS web app (browser) |
| `9999` | the appliance's own service port |

Choose whichever port your clients need with `--proxy-backend`. To relay several services at once, add more `frontend`/`backend` pairs to the generated `haproxy.cfg`, or edit the config template in `tunnel.py`.

---

## Checking that everything works

### Ubuntu

```sh
# 1. Tunnel service running?
systemctl is-active cpvpn                          # active

# 2. Tunnel interface up?
ip -brief addr | grep tun                          # tun0 UP <vpn-ip>

# 3. Server route goes through the tunnel?
ip route get 192.168.0.152                         # ... dev tun0 ...

# 4. Server reachable?
ping -c3 192.168.0.152
nc -vz -w3 192.168.0.152 80                        # succeeded

# 5. Relay listening?
sudo ss -lntp | grep 9999

# 6. Relay works end to end? (404 at "/" is OK)
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:9999/
```

### macOS

```sh
ifconfig | grep -A2 '^utun' | grep inet            # VPN interface has an IP
route -n get 192.168.0.152 | grep interface        # utunX
nc -vz -w3 192.168.0.152 80                        # open
pgrep -lf haproxy                                  # running
curl -s -o /dev/null -w "%{http_code}\n" http://$(ipconfig getifaddr en0):9999/
```

If the last command prints `404` or `200`, the relay works. If clients still can't connect, check the firewall ([Ubuntu](#3-open-the-firewall-if-ufw-is-active) / [macOS](#4-allow-haproxy-through-the-macos-firewall)) and whether the client is on the same network as the relay.

---

## macOS: internal Wi-Fi subnet overlap (`lanfix.py`)

### The problem

If the Mac's Wi-Fi uses **the same subnet as the office network** (e.g. both `192.168.0.0/24`), the VPN app sends almost every `192.168.0.x` address into the tunnel.
Clients on that Wi-Fi can still reach the Mac, but **the Mac's replies go into the VPN**, so connections time out.

```sh
route -n get 192.168.0.130 | grep interface    # utunX = replies to this local client are lost
```

### The fix

`lanfix.py` adds routes that are more specific than the VPN's, pointing back to Wi-Fi, for **every** `192.168.0.x` address **except the target server**, which stays on the VPN.
Three addresses (in the example: `.2`, `.3`, `.153`) are sent through the Wi-Fi router instead, because macOS can't route single addresses directly to the Wi-Fi.

```sh
python3 lanfix.py --dry-run        # preview; no changes, no sudo
sudo python3 lanfix.py             # add the routes
sudo python3 lanfix.py --undo      # remove them (VPN must still be connected)
```

Settings are at the top of `lanfix.py`:

```python
LAN = ipaddress.ip_network("192.168.0.0/24")   # office subnet that clashes
SERVER = ipaddress.ip_address("192.168.0.152") # stays on the VPN
WIFI = "en0"
```

Check it worked:

```sh
route -n get 192.168.0.130 | grep interface    # en0   (Wi-Fi)
route -n get 192.168.0.152 | grep interface    # utunX (VPN)
```

### Things to know

- **Run it again after every VPN reconnect.** Routes are also cleared when the Mac restarts.
- It **refuses to run unless the Mac's Wi-Fi is in `LAN`**. It isn't needed on other networks, such as a phone hotspot.
- While active, the **server is the only office machine the Mac can reach**. Use `--undo` to reach others.
- If the VPN app removes the routes or disconnects, it doesn't allow this workaround. Use the permanent fix.
- **Permanent fix:** change the Wi-Fi router's LAN range (e.g. to `192.168.1.x`). Then there's no overlap and `lanfix.py` isn't needed.

---

## Troubleshooting

### Messages you can ignore

| Message | Why it's harmless |
|---|---|
| macOS: `Error: Running Homebrew as root is extremely dangerous...` | The script tries `brew install` under sudo. HAProxy is already installed. |
| macOS: `Unload failed: 5: Input/output error` | The service wasn't loaded yet, so there was nothing to unload. |
| HAProxy `[WARNING] log format ignored...` / `started as root without any 'chroot'` | Informational only |
| `<ip> did not answer (expected if --no-vpn ...)` | The tunnel wasn't up yet when the ping ran. Test again. |

### Ubuntu

| Symptom | Cause | Fix |
|---|---|---|
| `pip: no such option: --break-system-packages` then `cpyvpn not importable` | Older pip (older Ubuntu releases) | `sudo python3 -m pip install --upgrade cpyvpn`, then re-run the installer |
| `cp_client not found after install` | pip installed it somewhere not on `PATH` | Find it with `sudo find / -name cp_client -type f 2>/dev/null`, make sure that folder is on root's `PATH`, then re-run |
| `Could not locate a vpnc-script` | `vpnc-scripts` is missing or in an unusual place | `sudo apt install vpnc-scripts`, or pass `--vpnc-script /path/to/vpnc-script` |
| `WARNING: could not patch cpyvpn run_vpnc()` | A newer `cpyvpn` changed its code | The tunnel may fail. Install an older `cpyvpn` version, or adapt `PATCHED_METHOD` in `tunnel.py`. |
| `cpvpn` keeps restarting | Wrong gateway, username or password, or the gateway can't be reached | `journalctl -u cpvpn -n 50`. Fix the value and re-run the installer. |
| Tunnel restarts **every minute** | The watchdog's ping to `CPVPN_TEST_IP` fails, even though the tunnel may be fine | Use a test IP that answers ping (`--test-ip`), or `--no-watchdog` |
| No `tun` interface after setup | The tunnel isn't up yet, or login failed | Wait a few seconds, then check `journalctl -u cpvpn -f` |
| `haproxy -c` fails during setup | Invalid `--proxy-listen` / `--proxy-backend` | Use the `ip:port` format |
| Relay works locally but clients time out | `ufw` or cloud firewall blocking port 9999 | See [Setup step 3](#3-open-the-firewall-if-ufw-is-active) |

### macOS

| Symptom | Cause | Fix |
|---|---|---|
| Clients time out | Firewall, client on a different network, Mac IP changed, or subnet overlap | Setup step 4. Same network. `ipconfig getifaddr en0`. `sudo python3 lanfix.py`. |
| Worked, then stopped after reconnecting the VPN | The VPN app reinstalled its routes | `sudo python3 lanfix.py` again |
| Other office machines unreachable from the Mac | `lanfix.py` routes still active | `sudo python3 lanfix.py --undo`, or restart the Mac |

### Both

| Symptom | Cause | Fix |
|---|---|---|
| Browser shows a **404** page | The relay works, but the app path is missing | `http://<relay-ip>:9999/<app-path>` |
| `ping` says **Destination Host Unreachable** from an office IP | The VPN works, but nothing is at that IP | Wrong or changed server IP, or the server is off. Confirm the IP. |
| Log says `Server relay_out/target is DOWN` | HAProxy can't reach the server | Check the tunnel and the backend IP/port. HAProxy re-checks every 10 s and recovers by itself. |
| Connections drop after about 1 min of inactivity | HAProxy idle timeout is `1m` | Increase `timeout client` / `timeout server` in the config template in `tunnel.py`, then re-run it |

---

## Uninstall

**Ubuntu**

```sh
sudo python3 tunnel.py --uninstall
```

This stops and removes the tunnel and watchdog services. It restores the original `haproxy.cfg` if one was backed up; otherwise it disables HAProxy. It also deletes `/etc/cpvpn/`.
The installed packages stay. To remove them too:

```sh
sudo apt remove haproxy vpnc-scripts
sudo python3 -m pip uninstall cpyvpn --break-system-packages
```

**macOS**

```sh
sudo python3 lanfix.py --undo        # only if lanfix routes are active (VPN connected)
sudo python3 tunnel.py --uninstall
HAP=$(realpath /opt/homebrew/bin/haproxy)
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --remove "$HAP"
brew uninstall haproxy               # optional
```

---

## Security notes

- **Never commit `.env`**: it can contain your VPN gateway, username and password. It's already in `.gitignore`.
- The VPN password is stored in plain text in `/etc/cpvpn/passwd.sh` (Ubuntu) or `/opt/homebrew/etc/cpvpn/passwd.sh` (macOS), readable by root only. On macOS relay-only setups it contains the placeholder `unused`.
- By default the relay listens on **all interfaces** (`0.0.0.0:9999`). **Anyone who can reach that port can reach the target service through your VPN.** Limit access with `--proxy-source` and the host firewall, and never expose port 9999 to the internet.
- Traffic between clients and the relay is **not encrypted** (plain HTTP/TCP). Only the relay → office part goes through the encrypted VPN.
- On macOS, HAProxy runs as root. On Ubuntu it runs as the `haproxy` user.
