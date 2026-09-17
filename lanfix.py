#!/usr/bin/env python3
"""
lanfix.py - make the relay reachable on the internal Wi-Fi (192.168.0.x) while
            the Check Point VPN client is connected.

The office LAN and the internal Wi-Fi both use 192.168.0.0/24, so the VPN client
routes 192.168.0.2-255 into the tunnel and this Mac's replies to local Wi-Fi
devices vanish. This adds routes one step more specific than each VPN route,
pointing back at Wi-Fi, for every address except SERVER (the host that
must stay reachable through the VPN, i.e. the relay's backend).

Re-run after every VPN (re)connect. Routes are gone after a reboot.

    python3 lanfix.py --dry-run     # show the plan (no root needed)
    sudo python3 lanfix.py          # apply
    sudo python3 lanfix.py --undo   # remove (VPN must still be connected)
"""
import argparse
import ipaddress
import subprocess
import sys

LAN = ipaddress.ip_network("192.168.0.0/24")
SERVER = ipaddress.ip_address("192.168.0.152")
WIFI = "en0"


def parse_dest(dest, flags):
    # netstat abbreviates: "192.168.0" = /24, "192.168.0.0" with H flag = /32
    addr, _, plen = dest.partition("/")
    octets = addr.split(".")
    if not plen:
        plen = 32 if "H" in flags else 8 * len(octets)
    return ipaddress.ip_network(".".join(octets + ["0"] * (4 - len(octets))) + f"/{plen}", strict=False)


def vpn_routes():
    out = subprocess.check_output(["netstat", "-rn", "-f", "inet"], text=True)
    for f in (line.split() for line in out.splitlines()):
        if len(f) >= 4 and f[0][0].isdigit() and f[3].startswith("utun"):
            try:
                n = parse_dest(f[0], f[2])
            except ValueError:
                continue
            if n.subnet_of(LAN) and n.prefixlen < 32:
                yield n


def plan():
    vpn = list(vpn_routes())
    if not vpn:
        sys.exit("No VPN routes for 192.168.0.x found - connect the VPN first.")
    server = ipaddress.ip_network(f"{SERVER}/32")
    wifi = [p for n in vpn for p in (n.address_exclude(server) if SERVER in n else n.subnets(1))]
    # self-check: longest match is Wi-Fi for every covered address, tunnel only for the server
    for ip in LAN:
        best = max((n for n in vpn + wifi if ip in n), key=lambda n: n.prefixlen, default=None)
        assert best is None or (best in vpn) == (ip == SERVER), ip
    return wifi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--undo", action="store_true")
    args = ap.parse_args()
    verb = "delete" if args.undo else "add"
    router = "<router>"
    if not (args.undo or args.dry_run):
        ip, router = (subprocess.run(["ipconfig", *a], capture_output=True, text=True).stdout.strip()
                      for a in (["getifaddr", WIFI], ["getoption", WIFI, "router"]))
        if not ip or ipaddress.ip_address(ip) not in LAN:
            sys.exit(f"Wi-Fi is on {ip or 'no network'}, not the internal Wi-Fi (192.168.0.x). Join it first.")
    for n in plan():
        if n.prefixlen == 32:
            # ponytail: "-host X -interface en0" pins X to this Mac's own MAC, so /32s hairpin via the router
            cmd = ["route", "-n", verb, "-host", str(n.network_address)] + ([] if args.undo else [router])
        else:
            cmd = ["route", "-n", verb, "-net", str(n)] + ([] if args.undo else ["-interface", WIFI])
        print(" ".join(cmd))
        if not args.dry_run:
            subprocess.run(cmd, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
