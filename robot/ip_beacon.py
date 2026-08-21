#!/usr/bin/env python3
"""
ip_beacon.py - tell the cloud where this robot currently is on the network.

The Orin's campus-WiFi address is a DHCP lease and it moves (10.1.125.24 ->
10.1.122.128 -> 10.1.122.235 so far). mDNS cannot solve this: the laptop and
the dog land in different /21s and mDNS is link-local multicast, so `.local`
names only work on the wired dog net. Until NP IT reserves the WiFi MAC
(00:2e:2d:ad:3c:8d), this beacon is how you find the dog without walking to it.

Writes s3://argus-frames-506868652945/ops/orin/orin_ip.json every run:
    {"hostname": ..., "updated_utc": ..., "uptime": ...,
     "addresses": {"wlan0": "10.1.122.235", "eth1": "192.168.123.18", ...}}

Read it from anywhere with:
    aws s3 cp s3://argus-frames-506868652945/ops/orin/orin_ip.json - --profile prod

Installed as a user cron job (no root needed):
    @reboot        sleep 60 && /usr/bin/python3 /home/unitree/go2/ip_beacon.py
    */5 * * * *    /usr/bin/python3 /home/unitree/go2/ip_beacon.py
The @reboot entry sleeps first because wlan0 has no lease for ~45 s after boot.

Failure policy: silent. A beacon that cannot reach S3 must never be noisy or
block anything - it just records the reason in ~/go2/.ip_beacon_last and exits.
"""
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone

BUCKET = "argus-frames-506868652945"
KEY    = "ops/orin/orin_ip.json"
REGION = "us-east-1"
STATUS = os.path.expanduser("~/go2/.ip_beacon_last")


def addresses():
    """Every non-loopback IPv4 address, keyed by interface. Parsed from
    `ip -4 -o addr` rather than a library so this has no dependency beyond
    boto3, which the patrol scripts already need."""
    out = {}
    try:
        raw = subprocess.check_output(["ip", "-4", "-o", "addr"],
                                      stderr=subprocess.DEVNULL).decode()
    except Exception:
        return out
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        iface, cidr = parts[1], parts[3]
        if iface == "lo":
            continue
        out[iface] = cidr.split("/")[0]
    return out


def uptime():
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        return "%dh%02dm" % (secs // 3600, (secs % 3600) // 60)
    except Exception:
        return "unknown"


def record(msg):
    """Overwrite (never append) the one-line status, so this file cannot grow."""
    try:
        with open(STATUS, "w") as f:
            f.write("%s  %s\n" % (datetime.now(timezone.utc).isoformat(), msg))
    except Exception:
        pass


def main():
    payload = {
        "hostname":    socket.gethostname(),
        "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "uptime":      uptime(),
        "addresses":   addresses(),
        "ssh":         "ssh unitree@<wlan0 address above>",
    }
    try:
        import boto3
        boto3.client("s3", region_name=REGION).put_object(
            Bucket=BUCKET, Key=KEY,
            Body=json.dumps(payload, indent=2).encode(),
            ContentType="application/json")
    except Exception as e:
        record("FAILED: %s" % e)
        sys.exit(0)          # silent by design
    record("ok -> %s" % payload["addresses"].get("wlan0", "no wlan0"))


if __name__ == "__main__":
    main()
