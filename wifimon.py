#!/usr/bin/env python3
"""
wifimon — full-screen temporal Wi-Fi RF monitor for Linux.

Watches the air so that when a device (e.g. a phone across the room) drops
off Wi-Fi, you can glance at this screen and see whether there was a
corresponding RF-level event: our own link RSSI dipping, frame retries
spiking, beacons going missing, airtime saturating, or an AP vanishing.

Panels:
  * 2.4 GHz spectrum  — visible APs by channel: bar = strongest signal,
                        bottom row = AP count per channel
  * 5 GHz spectrum    — same for the 5 GHz band
  * Timeline (large)  — last N minutes, 1 column = 1 second:
        rssi    our link signal (dBm); red x = disconnected. Bars are
                tinted by band (2.4=yellow, 5=cyan, 6=magenta) to match
                the band/chan lane, so a roam recolors the signal too.
        band/chan  which band + channel we're on; the label is written
                at each change (a few seconds) then a continuation rule
                runs until the next change — makes band/channel hops
                (mesh roaming) obvious. Same per-band colors.
        router   RTT of a 1 Hz ping to the router — the Wi-Fi hop in
                 isolation; red ✕ = router unreachable while associated
                 (the "bars lie" moment)
        internet RTT of a 1 Hz ping to 1.1.1.1 — the whole path;
                 magenta ✕ = router fine but internet lost (the problem
                 is past the router)
        traffic  our own throughput (rx+tx Mb/s) — if latency/retries
                 climb with it, congestion is self-inflicted; if they're
                 bad while it's flat, blame the air
        retry%   tx retransmission rate — high = hostile air / contention
        beac%    beacon delivery rate — low = we're missing AP beacons
        busy%/noise shown instead when the driver provides survey data
  * Event log         — timestamped anomalies

Everything is logged to CSV under ./logs/ for later analysis.

Usage:
    ./wifimon.py [--iface IFACE] [--scan-interval SEC]
                 [--headless SECONDS] [--debug-once]

Run as a normal user (scans go through NetworkManager) or with sudo
(direct nl80211 scans; on some drivers root also unlocks survey
noise/busy data).

Requires: python3 (stdlib only), iw, and optionally nmcli.
Keys: q quit, p pause/resume display (logging continues).
"""

import argparse
import collections
import csv
import curses
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

# ---------------------------------------------------------------- config

RSSI_MIN, RSSI_MAX = -95, -25          # display range, dBm
NOISE_MIN, NOISE_MAX = -105, -60
HISTORY_SECONDS = 7200                  # samples kept in memory
BAR = "▁▂▃▄▅▆▇█"

RETRY_EVENT_PCT = 70                    # sustained retry% considered an event
RETRY_EVENT_MIN_PKTS = 20               # ...only if we actually sent traffic
BEACON_EVENT_PCT = 50                   # beacon delivery below this = event

EVT_DISCONNECT = "DISCONNECT"
EVT_RECONNECT = "RECONNECT"
EVT_ROAM = "ROAM"
EVT_RSSI_DROP = "RSSI DROP"
EVT_NOISE = "NOISE SPIKE"
EVT_BUSY = "AIRTIME"
EVT_RETRY = "RETRY STORM"
EVT_BEACON = "BEACON LOSS"
EVT_AP_LOST = "AP LOST"
EVT_STALL = "LINK STALL"               # associated but gateway unreachable
EVT_INET = "INET LOSS"                 # gateway fine, internet not
EVT_LAG = "LAG"                        # internet reachable but very slow

INET_TARGET = "1.1.1.1"
LAG_EVENT_MS = 400                      # sustained RTT above this = event

# ---------------------------------------------------------------- helpers


def run(cmd, timeout=15):
    """Run a command, return stdout ('' on any failure)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def freq_to_channel(freq):
    freq = int(freq)
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if freq == 2484:
        return 14
    if 5000 < freq < 5925:
        return (freq - 5000) // 5
    if 5955 <= freq <= 7115:            # 6 GHz
        return (freq - 5950) // 5
    return 0


def band_of(freq):
    if freq < 3000:
        return "2.4"
    if freq < 5925:
        return "5"
    return "6"


def detect_iface():
    out = run(["iw", "dev"])
    m = re.findall(r"Interface\s+(\S+)", out)
    if m:
        return m[0]
    out = run(["nmcli", "-t", "-f", "DEVICE,TYPE", "dev"])
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "wifi":
            return parts[0]
    return None


# ---------------------------------------------------------------- collectors


class LinkPoller:
    """1 Hz: our own association state — the canary in the coal mine."""

    def __init__(self, iface):
        self.iface = iface

    def poll(self):
        out = run(["iw", "dev", self.iface, "link"], timeout=5)
        if not out or out.startswith("Not connected"):
            return {"connected": False}
        d = {"connected": True}
        m = re.search(r"Connected to ([0-9a-f:]{17})", out)
        d["bssid"] = m.group(1) if m else "?"
        m = re.search(r"SSID:\s*(.+)", out)
        d["ssid"] = m.group(1).strip() if m else "?"
        m = re.search(r"freq:\s*([\d.]+)", out)
        d["freq"] = int(float(m.group(1))) if m else 0
        m = re.search(r"signal:\s*(-?\d+)\s*dBm", out)
        d["rssi"] = int(m.group(1)) if m else None
        m = re.search(r"tx bitrate:\s*([\d.]+)\s*MBit/s", out)
        d["txrate"] = float(m.group(1)) if m else None
        return d


class StationPoller:
    """1 Hz: counters from `iw station dump` — retries, failures, beacons.

    The counters are cumulative since association; we convert them to
    per-second deltas.  retry% = retransmissions per transmitted packet,
    beacon% = beacons actually received vs. expected from the beacon
    interval.  Counters reset on roam/reconnect; negative deltas mean
    "new association", so we just re-baseline.
    """

    FIELDS = {
        "tx_packets": r"tx packets:\s*(\d+)",
        "tx_retries": r"tx retries:\s*(\d+)",
        "tx_failed": r"tx failed:\s*(\d+)",
        "beacon_rx": r"beacon rx:\s*(\d+)",
        "beacon_loss": r"beacon loss:\s*(\d+)",
        "rx_drop": r"rx drop misc:\s*(\d+)",
        "beacon_int": r"beacon interval:\s*(\d+)",
    }

    WINDOW = 5                          # seconds of deltas to smooth over

    def __init__(self, iface):
        self.iface = iface
        self._prev = None               # (time, counters, bssid)
        self._window = collections.deque(maxlen=self.WINDOW)

    def poll(self):
        out = run(["iw", "dev", self.iface, "station", "dump"], timeout=5)
        m = re.search(r"Station ([0-9a-f:]{17})", out)
        if not m:
            self._prev = None
            self._window.clear()
            return {}
        bssid = m.group(1)
        cur = {}
        for key, pat in self.FIELDS.items():
            mm = re.search(pat, out)
            if mm:
                cur[key] = int(mm.group(1))
        now = time.time()
        result = {}
        mm = re.search(r"signal avg:\s*(-?\d+)", out)
        if mm:
            result["rssi_avg"] = int(mm.group(1))

        prev = self._prev
        self._prev = (now, cur, bssid)
        if not prev or prev[2] != bssid:
            self._window.clear()        # new association: re-baseline
            return result
        dt = now - prev[0]
        if dt <= 0:
            return result
        d = {k: cur.get(k, 0) - prev[1].get(k, 0) for k in cur
             if k != "beacon_int"}
        # iwlwifi rewinds the beacon-rx counter every few seconds; that is
        # not a reassociation, so just skip the beacon metric this tick
        if d.get("beacon_rx", 0) < 0:
            d["beacon_rx"] = None
        if any(v is not None and v < 0 for v in d.values()):
            self._window.clear()        # counters reset (reassociation)
            return result

        result["tx_pkts"] = d.get("tx_packets", 0)
        result["tx_failed"] = d.get("tx_failed", 0)
        result["rx_drop"] = d.get("rx_drop", 0)
        result["beacon_loss"] = d.get("beacon_loss", 0)

        # smooth retry%/beacon% over a rolling window: per-second deltas
        # are dominated by quantization noise (a single retried frame, or
        # 9-vs-10 beacons in a tick, swings the raw ratio wildly)
        self._window.append((dt, d))
        sum_tx = sum(x[1].get("tx_packets", 0) for x in self._window)
        sum_retry = sum(x[1].get("tx_retries", 0) for x in self._window)
        result["win_tx_pkts"] = sum_tx
        if sum_tx > 0:
            result["retry_pct"] = round(
                min(100.0, 100.0 * sum_retry / sum_tx), 1)
        # beacon%: only over ticks whose beacon counter was valid
        beacon_dt = sum(x[0] for x in self._window
                        if x[1].get("beacon_rx") is not None)
        sum_beacon = sum(x[1].get("beacon_rx") or 0 for x in self._window)
        interval_tu = cur.get("beacon_int", 100) or 100
        expected = beacon_dt * 1000.0 / (interval_tu * 1.024)
        if expected > 0:
            result["beacon_pct"] = round(
                min(100.0, 100.0 * sum_beacon / expected), 1)
        return result


class SurveyPoller:
    """Noise floor + channel airtime, when the driver provides it.

    Many laptop radios (e.g. Intel iwlwifi) return nothing here without
    root — or nothing at all.  wifimon degrades gracefully: the timeline
    shows retry%/beacon% instead, which every driver supports.
    """

    def __init__(self, iface):
        self.iface = iface
        self._last = {}                 # freq -> (active_ms, busy_ms)

    def poll(self):
        out = run(["iw", "dev", self.iface, "survey", "dump"], timeout=5)
        result = {}                     # freq -> {"noise":, "busy_pct":}
        for block in out.split("Survey data from")[1:]:
            m = re.search(r"frequency:\s*([\d.]+) MHz", block)
            if not m:
                continue
            freq = int(float(m.group(1)))
            in_use = "[in use]" in block
            entry = {"in_use": in_use, "noise": None, "busy_pct": None}
            m = re.search(r"noise:\s*(-?\d+)\s*dBm", block)
            if m:
                entry["noise"] = int(m.group(1))
            ma = re.search(r"channel active time:\s*(\d+) ms", block)
            mb = re.search(r"channel busy time:\s*(\d+) ms", block)
            if ma and mb:
                active, busy = int(ma.group(1)), int(mb.group(1))
                if freq in self._last:
                    da = active - self._last[freq][0]
                    db = busy - self._last[freq][1]
                    if da > 0 and 0 <= db <= da:
                        entry["busy_pct"] = round(100.0 * db / da, 1)
                self._last[freq] = (active, busy)
            result[freq] = entry
        return result


class TrafficPoller:
    """1 Hz: our own throughput from the kernel byte counters — free to
    read, no permissions. Separates self-inflicted congestion (lag rises
    with our own load: bufferbloat) from ambient hostility (retries
    while idle: neighbors / interferers)."""

    def __init__(self, iface):
        self.base = f"/sys/class/net/{iface}/statistics"
        self._prev = None               # (time, rx_bytes, tx_bytes)

    def _read(self, name):
        try:
            with open(os.path.join(self.base, name)) as f:
                return int(f.read())
        except (OSError, ValueError):
            return None

    def poll(self):
        rx, tx = self._read("rx_bytes"), self._read("tx_bytes")
        now = time.time()
        prev = self._prev
        self._prev = (now, rx, tx)
        if rx is None or tx is None or prev is None or prev[1] is None:
            return {}
        dt = now - prev[0]
        drx, dtx = rx - prev[1], tx - prev[2]
        if dt <= 0 or drx < 0 or dtx < 0:   # interface bounced
            return {}
        return {"rx_mbps": round(drx * 8 / dt / 1e6, 2),
                "tx_mbps": round(dtx * 8 / dt / 1e6, 2)}


class Pinger(threading.Thread):
    """Persistent 1 Hz ping to one target; the end-to-end truth serum.

    Runs a single long-lived `ping` process and parses replies as they
    arrive. sample() classifies the current second as ok (with RTT),
    warmup (just started / no target yet), or loss.
    """

    def __init__(self, target=None):
        super().__init__(daemon=True)
        self.target = target
        self.rtt = None
        self.last_reply = 0.0
        self._proc_started = None       # None until the first ping launches
        self._proc = None

    def set_target(self, target):
        if target != self.target:
            self.target = target
            if self._proc:
                self._proc.kill()       # reader loop restarts with new target

    def run(self):
        while True:
            target = self.target
            if not target:
                time.sleep(1)
                continue
            try:
                self._proc = subprocess.Popen(
                    ["ping", "-n", "-i", "1", "-W", "2", target],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True)
            except OSError:
                time.sleep(5)
                continue
            self._proc_started = time.time()
            for line in self._proc.stdout:
                if target != self.target:
                    break
                m = re.search(r"time=([\d.]+)\s*ms", line)
                if m:
                    self.rtt = float(m.group(1))
                    self.last_reply = time.time()
            self._proc.kill()
            time.sleep(2)               # then restart (network may be back)

    def sample(self):
        now = time.time()
        if now - self.last_reply <= 2.0:
            return "ok", self.rtt
        if (not self.target or self._proc_started is None
                or now - self._proc_started < 3.0):
            return "warmup", None
        return "loss", None


def default_gateway(iface):
    out = run(["ip", "route", "show", "default"])
    best = None
    for line in out.splitlines():
        m = re.search(r"default via (\S+) dev (\S+)", line)
        if m:
            if m.group(2) == iface:
                return m.group(1)
            best = best or m.group(1)
    return best


class Scanner(threading.Thread):
    """Background AP scanner. Direct nl80211 scan when root, otherwise asks
    NetworkManager to rescan and reads the cached results."""

    def __init__(self, iface, interval):
        super().__init__(daemon=True)
        self.iface = iface
        self.interval = interval
        self.lock = threading.Lock()
        self.aps = {}                   # bssid -> ap dict
        self.last_scan = 0
        self.new_results = False
        self._is_root = os.geteuid() == 0
        self._has_nmcli = shutil.which("nmcli") is not None

    def run(self):
        while True:
            self._scan_once()
            time.sleep(self.interval)

    def _scan_once(self):
        out = ""
        if self._is_root:
            out = run(["iw", "dev", self.iface, "scan"], timeout=25)
        if not out:
            if self._has_nmcli and not self._is_root:
                run(["nmcli", "device", "wifi", "rescan", "ifname", self.iface],
                    timeout=15)
                time.sleep(3)           # let the scan complete
            out = run(["iw", "dev", self.iface, "scan", "dump"], timeout=10)
        if not out:
            return
        aps = {}
        now = time.time()
        for block in re.split(r"^BSS ", out, flags=re.M)[1:]:
            m = re.match(r"([0-9a-f:]{17})", block)
            if not m:
                continue
            bssid = m.group(1)
            ap = {"bssid": bssid, "seen": now}
            m = re.search(r"last seen:\s*(\d+) ms ago", block)
            if m:
                ap["seen"] = now - int(m.group(1)) / 1000.0
            m = re.search(r"freq:\s*([\d.]+)", block)
            ap["freq"] = int(float(m.group(1))) if m else 0
            m = re.search(r"signal:\s*(-?[\d.]+)\s*dBm", block)
            ap["signal"] = float(m.group(1)) if m else None
            m = re.search(r"^\s*SSID:\s*(.*)$", block, flags=re.M)
            ap["ssid"] = (m.group(1).strip() if m else "") or "<hidden>"
            if ap["freq"] and ap["signal"] is not None:
                aps[bssid] = ap
        if aps:
            with self.lock:
                self.aps = aps
                self.last_scan = now
                self.new_results = True

    def snapshot(self):
        with self.lock:
            fresh = self.new_results
            self.new_results = False
            return dict(self.aps), self.last_scan, fresh


# ---------------------------------------------------------------- nodes


class NodeMap:
    """Groups BSSIDs into physical access points ("nodes") and gives them
    stable friendly names: #1, #2, ...

    In a mesh (e.g. Synology WiFi Point), every node broadcasts the same
    SSID but each node's each radio has its own BSSID — and the radios of
    one physical box share the first five MAC octets. That prefix is the
    grouping key.

    The mapping persists in ap-nodes.json next to the tool. Edit the
    "name" values there to rename nodes (e.g. "#2" -> "attic") — the
    names appear in the status line, roam events, and wifianalyze.
    """

    def __init__(self, path="ap-nodes.json"):
        self.path = path
        self.nodes = {}                 # key -> {"name","ssid","seen_bssid"}
        try:
            with open(path) as f:
                self.nodes = json.load(f)
        except (OSError, ValueError):
            self.nodes = {}

    @staticmethod
    def key(bssid):
        try:
            parts = bssid.lower().split(":")
            first = int(parts[0], 16) & ~0x02   # ignore locally-admin bit
            return f"{first:02x}:" + ":".join(parts[1:5])
        except (ValueError, IndexError, AttributeError):
            return None

    def register(self, bssid, ssid):
        k = self.key(bssid)
        if not k or not ssid:
            return None
        if k not in self.nodes:
            n = sum(1 for v in self.nodes.values()
                    if v.get("ssid") == ssid) + 1
            self.nodes[k] = {"name": f"#{n}", "ssid": ssid,
                             "seen_bssid": bssid}
            try:
                with open(self.path, "w") as f:
                    json.dump(self.nodes, f, indent=1)
            except OSError:
                pass
        return self.nodes[k]["name"]

    def label(self, bssid):
        k = self.key(bssid) if bssid else None
        v = self.nodes.get(k) if k else None
        return v["name"] if v else None


# ---------------------------------------------------------------- logging


class CsvLogs:
    def __init__(self, directory):
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.link_path = os.path.join(directory, f"link-{stamp}.csv")
        self.scan_path = os.path.join(directory, f"scan-{stamp}.csv")
        self.event_path = os.path.join(directory, f"events-{stamp}.csv")
        self._link = open(self.link_path, "w", newline="")
        self._scan = open(self.scan_path, "w", newline="")
        self._event = open(self.event_path, "w", newline="")
        self.link_w = csv.writer(self._link)
        self.scan_w = csv.writer(self._scan)
        self.event_w = csv.writer(self._event)
        self.link_w.writerow(["time", "connected", "ssid", "bssid", "freq",
                              "rssi_dbm", "txrate_mbps", "retry_pct",
                              "beacon_pct", "tx_pkts", "tx_failed",
                              "beacon_loss", "rx_drop",
                              "noise_dbm", "busy_pct",
                              "gw_rtt_ms", "inet_rtt_ms",
                              "gw_loss", "inet_loss",
                              "rx_mbps", "tx_mbps"])
        self.scan_w.writerow(["time", "bssid", "ssid", "freq", "chan", "signal_dbm"])
        self.event_w.writerow(["time", "event", "detail"])

    def flush(self):
        for f in (self._link, self._scan, self._event):
            f.flush()


# ---------------------------------------------------------------- monitor


class Monitor:
    def __init__(self, iface, scan_interval, logdir):
        self.iface = iface
        self.link = LinkPoller(iface)
        self.station = StationPoller(iface)
        self.survey = SurveyPoller(iface)
        self.scanner = Scanner(iface, scan_interval)
        self.ping_gw = Pinger()
        self.ping_inet = Pinger(INET_TARGET)
        self.traffic = TrafficPoller(iface)
        self.nodemap = NodeMap()
        self.logs = CsvLogs(logdir)
        self.history = collections.deque(maxlen=HISTORY_SECONDS)
        self.events = collections.deque(maxlen=400)
        self.prev = {}                  # previous link sample
        self.rssi_window = collections.deque(maxlen=6)
        self.known_aps = {}             # bssid -> last ap dict (for AP-lost)
        self._gw_bad = 0                # consecutive seconds of gateway loss
        self._inet_bad = 0
        self._lag_high = 0
        self._gw_refresh = 0

    def start(self):
        self.scanner.start()
        self.ping_gw.start()
        self.ping_inet.start()

    def event(self, kind, detail=""):
        ts = datetime.now()
        self.events.append((ts, kind, detail))
        self.logs.event_w.writerow([ts.isoformat(timespec="seconds"), kind, detail])

    # -- one 1 Hz tick: collect, detect, log ------------------------------
    def tick(self):
        now = datetime.now()
        link = self.link.poll()
        stat = self.station.poll() if link.get("connected") else {}
        surveys = self.survey.poll()

        cur = surveys.get(link.get("freq"), {}) if link.get("connected") else {}
        if not cur:                     # fall back to any in-use channel
            for entry in surveys.values():
                if entry.get("in_use"):
                    cur = entry
                    break
        noise = cur.get("noise")
        busy = cur.get("busy_pct")
        retry = stat.get("retry_pct")
        beacon = stat.get("beacon_pct")

        # ---- end-to-end probes (gateway + internet)
        if link.get("connected") and time.time() - self._gw_refresh > 10:
            self.ping_gw.set_target(default_gateway(self.iface))
            self._gw_refresh = time.time()
        gw_state, gw_ms = self.ping_gw.sample()
        inet_state, inet_ms = self.ping_inet.sample()
        tput = self.traffic.poll()
        rx_mbps, tx_mbps = tput.get("rx_mbps"), tput.get("tx_mbps")
        if not link.get("connected"):   # offline: losses are not news
            gw_state = inet_state = "warmup"
        gw_loss = gw_state == "loss"
        inet_loss = inet_state == "loss"
        self._gw_bad = self._gw_bad + 1 if gw_loss else 0
        self._inet_bad = self._inet_bad + 1 if inet_loss else 0
        self._lag_high = self._lag_high + 1 if (
            inet_ms is not None and inet_ms > LAG_EVENT_MS) else 0

        # ---- event detection
        if link.get("connected"):
            self.nodemap.register(link.get("bssid"), link.get("ssid"))

        def node_of(lnk):
            return self.nodemap.label(lnk.get("bssid")) or "?"

        was = self.prev
        if was:
            if was.get("connected") and not link["connected"]:
                self.event(EVT_DISCONNECT,
                           f"lost {was.get('ssid')} {node_of(was)} "
                           f"({was.get('bssid')})")
            elif not was.get("connected") and link["connected"]:
                self.event(EVT_RECONNECT,
                           f"{link.get('ssid')} {node_of(link)} ch "
                           f"{freq_to_channel(link.get('freq', 0))}")
            elif (link.get("connected") and was.get("bssid")
                  and link.get("bssid") != was.get("bssid")):
                self.event(EVT_ROAM,
                           f"{node_of(was)} ch "
                           f"{freq_to_channel(was.get('freq', 0))} -> "
                           f"{node_of(link)} ch "
                           f"{freq_to_channel(link.get('freq', 0))} "
                           f"({was.get('bssid')} -> {link.get('bssid')})")
        if link.get("rssi") is not None:
            self.rssi_window.append(link["rssi"])
            if (len(self.rssi_window) == self.rssi_window.maxlen
                    and self.rssi_window[0] - link["rssi"] >= 12):
                self.event(EVT_RSSI_DROP,
                           f"{self.rssi_window[0]} -> {link['rssi']} dBm in "
                           f"{self.rssi_window.maxlen}s")
                self.rssi_window.clear()
        if (retry is not None and retry >= RETRY_EVENT_PCT
                and stat.get("win_tx_pkts", 0) >= RETRY_EVENT_MIN_PKTS
                and not self._recent_event(EVT_RETRY, 30)):
            self.event(EVT_RETRY,
                       f"{retry:.0f}% retries over {stat['win_tx_pkts']} pkts")
        if (beacon is not None and beacon < BEACON_EVENT_PCT
                and not self._recent_event(EVT_BEACON, 30)):
            self.event(EVT_BEACON, f"only {beacon:.0f}% of beacons received")
        if stat.get("beacon_loss"):
            if not self._recent_event(EVT_BEACON, 30):
                self.event(EVT_BEACON,
                           f"driver reported beacon loss x{stat['beacon_loss']}")
        if self._gw_bad == 3 and not self._recent_event(EVT_STALL, 30):
            self.event(EVT_STALL,
                       f"associated (rssi {link.get('rssi')}) but gateway "
                       f"unreachable — the 'bars lie' moment")
        if (self._inet_bad == 5 and not gw_loss
                and not self._recent_event(EVT_INET, 30)):
            self.event(EVT_INET, "gateway fine but internet unreachable "
                       "(problem is past the router)")
        if self._lag_high == 5 and not self._recent_event(EVT_LAG, 30):
            self.event(EVT_LAG, f"internet RTT {inet_ms:.0f} ms sustained")
        if noise is not None and noise > -80:
            if not self._recent_event(EVT_NOISE, 30):
                self.event(EVT_NOISE, f"noise floor {noise} dBm")
        if busy is not None and busy > 85:
            if not self._recent_event(EVT_BUSY, 30):
                self.event(EVT_BUSY, f"channel {busy:.0f}% busy")

        # ---- scan bookkeeping (AP appearance/disappearance)
        aps, last_scan, fresh = self.scanner.snapshot()
        if fresh and link.get("ssid"):
            # every AP broadcasting our SSID is one of our mesh nodes
            for ap in aps.values():
                if ap["ssid"] == link["ssid"]:
                    self.nodemap.register(ap["bssid"], link["ssid"])
        if fresh:
            for bssid, ap in aps.items():
                self.logs.scan_w.writerow([now.isoformat(timespec="seconds"),
                                           bssid, ap["ssid"], ap["freq"],
                                           freq_to_channel(ap["freq"]),
                                           ap["signal"]])
            # debounce AP-lost: scans (especially cached NM dumps) are often
            # partial, so require an AP to miss two consecutive scans
            for bssid, ap in aps.items():
                self.known_aps[bssid] = dict(ap, misses=0)
            for bssid, ap in list(self.known_aps.items()):
                if bssid in aps:
                    continue
                ap["misses"] = ap.get("misses", 0) + 1
                if ap["misses"] >= 2:
                    # only strong APs are worth an event; weak ones flicker
                    # in and out of scan range constantly
                    if ap["signal"] >= -65:
                        self.event(EVT_AP_LOST,
                                   f"{ap['ssid']} ({bssid}) "
                                   f"ch {freq_to_channel(ap['freq'])} "
                                   f"was {ap['signal']:.0f} dBm")
                    del self.known_aps[bssid]

        # ---- history + logs
        sample = {
            "ts": now, "connected": link.get("connected", False),
            "node": (self.nodemap.label(link.get("bssid"))
                     if link.get("connected") else None),
            "rssi": link.get("rssi"), "noise": noise, "busy": busy,
            "retry": retry, "beacon": beacon,
            "gw_ms": gw_ms, "inet_ms": inet_ms,
            "gw_loss": gw_loss, "inet_loss": inet_loss,
            "rx_mbps": rx_mbps, "tx_mbps": tx_mbps,
            "mbps": (rx_mbps + tx_mbps
                     if rx_mbps is not None and tx_mbps is not None else None),
            "ssid": link.get("ssid"), "bssid": link.get("bssid"),
            "freq": link.get("freq"), "txrate": link.get("txrate"),
            "event": self.events[-1][1] if self.events and
            (now - self.events[-1][0]).total_seconds() < 1.5 else None,
        }
        self.history.append(sample)

        def nz(v):
            return v if v is not None else ""
        self.logs.link_w.writerow([
            now.isoformat(timespec="seconds"),
            int(sample["connected"]), sample["ssid"] or "",
            sample["bssid"] or "", sample["freq"] or "",
            nz(sample["rssi"]), nz(sample["txrate"]), nz(retry), nz(beacon),
            nz(stat.get("tx_pkts")), nz(stat.get("tx_failed")),
            nz(stat.get("beacon_loss")), nz(stat.get("rx_drop")),
            nz(noise), nz(busy),
            nz(gw_ms), nz(inet_ms), int(gw_loss), int(inet_loss),
            nz(rx_mbps), nz(tx_mbps)])
        self.logs.flush()
        self.prev = link
        # display from the debounced union of the last two scans — raw
        # single-scan results (especially cached NM dumps) are partial and
        # make the spectrum panels flicker
        return sample, (self.known_aps or aps), last_scan

    def _recent_event(self, kind, seconds):
        now = datetime.now()
        return any(k == kind and (now - t).total_seconds() < seconds
                   for t, k, _ in self.events)


# ---------------------------------------------------------------- drawing


def scale(value, lo, hi, steps):
    """Map value in [lo, hi] to 0..steps-1."""
    if value is None:
        return None
    v = max(lo, min(hi, value))
    return int(round((v - lo) / (hi - lo) * (steps - 1)))


def draw_spectrum(win, title, aps, channels, own_bssid, color_own, color_other,
                  color_hot=0):
    """One band panel: per-channel bars of the strongest AP (RSSI on top),
    channel numbers below, and the AP count per channel on the bottom row."""
    h, w = win.getmaxyx()
    win.erase()
    win.box()
    win.addnstr(0, 2, f" {title} ", w - 4, curses.A_BOLD)
    bar_h = h - 5                       # rows available for bars
    if bar_h < 2 or w < 20:
        win.noutrefresh()
        return

    by_chan = {}                        # strongest AP per channel
    counts = collections.Counter()      # APs per channel
    for ap in aps:
        ch = freq_to_channel(ap["freq"])
        if ch in channels:
            counts[ch] += 1
            cur = by_chan.get(ch)
            if cur is None or ap["signal"] > cur["signal"]:
                by_chan[ch] = ap

    bar_base = h - 4                    # lowest bar row
    chan_row = h - 3                    # channel numbers
    count_row = h - 2                   # AP count per channel
    slot_w = max(3, (w - 4) // max(1, len(channels)))
    x = 2
    for ch in channels:
        if x + slot_w >= w - 1:
            break
        win.addnstr(chan_row, x, str(ch).rjust(2), slot_w, curses.A_DIM)
        n = counts.get(ch, 0)
        if n:
            attr = (color_hot | curses.A_BOLD) if n >= 5 else curses.A_DIM
            win.addnstr(count_row, x, str(n).rjust(2), slot_w, attr)
        ap = by_chan.get(ch)
        if ap:
            lvl = scale(ap["signal"], RSSI_MIN, RSSI_MAX, bar_h * 8)
            full, part = divmod(lvl, 8)
            attr = color_own if ap["bssid"] == own_bssid else color_other
            col = x + max(0, (2 - 1) // 2)
            for i in range(full):
                win.addstr(bar_base - i, col, "█", attr)
            if part and full < bar_h:
                win.addstr(bar_base - full, col, BAR[part - 1], attr)
            # dBm at top of bar
            top = bar_base - min(bar_h - 1, full + (1 if part else 0))
            if top >= 1:
                win.addnstr(top - 1 if top > 1 else 1, max(2, col - 1),
                            f"{ap['signal']:.0f}"[1:], 3, curses.A_DIM)
        x += slot_w
    win.noutrefresh()


GUTTER = 15                             # left axis gutter width
RIGHT = 7                               # right current-value column


def band_color(freq, color_map):
    """Per-band color, matching the band/channel lane:
    2.4 = yellow, 5 = cyan, 6 = magenta. None if unknown."""
    b = band_of(freq) if freq else None
    return {"2.4": color_map["busy"], "5": color_map["lag"],
            "6": color_map["noise"]}.get(b)


def draw_chart(win, y0, rows, samples, getter, lo, hi, attr,
               label, unit, disconnect_attr=None, bad=None, attr_of=None):
    """A multi-row column chart: one terminal column per sample.

    bad: optional callable(sample) -> None when the second was fine, or a
    curses attr to draw a full-height ✕ column marker in (so different
    failure causes can get different colors). Visually distinct from
    "no data" (blank).
    attr_of: optional callable(sample) -> attr to color each column
    individually (falling back to `attr` when it returns None); used to
    tint the RSSI bars by band.
    """
    h, w = win.getmaxyx()
    x0 = GUTTER
    n = min(len(samples), w - GUTTER - RIGHT)
    samples = samples[-n:] if n > 0 else []
    # axis gutter: "label hi│" on top row, "lo│" on bottom row
    top_lab = f"{label} {hi}"[:GUTTER - 2]
    win.addnstr(y0, 0, top_lab.rjust(GUTTER - 2) + " │", GUTTER, curses.A_DIM)
    for r in range(1, rows - 1):
        win.addnstr(y0 + r, GUTTER - 2, " │", 2, curses.A_DIM)
    if rows > 1:
        win.addnstr(y0 + rows - 1, 0,
                    f"{lo}"[:GUTTER - 2].rjust(GUTTER - 2) + " │",
                    GUTTER, curses.A_DIM)
    last_val = None
    for i, s in enumerate(samples):
        x = x0 + i
        if disconnect_attr is not None and not s["connected"]:
            try:
                win.addstr(y0 + rows - 1, x, "x", disconnect_attr | curses.A_BOLD)
            except curses.error:
                pass
            continue
        battr = bad(s) if bad is not None else None
        if battr is not None:
            try:
                win.addstr(y0, x, "✕", battr | curses.A_BOLD)
                for r in range(1, rows):
                    win.addstr(y0 + r, x, "│", battr)
            except curses.error:
                pass
            continue
        v = getter(s)
        if v is None:
            continue
        last_val = v
        a = (attr_of(s) or attr) if attr_of else attr
        lvl = scale(v, lo, hi, rows * 8)
        full, part = divmod(lvl, 8)
        try:
            for r in range(full):
                win.addstr(y0 + rows - 1 - r, x, "█", a)
            if part:
                win.addstr(y0 + rows - 1 - full, x, BAR[part - 1], a)
            elif full == 0:
                win.addstr(y0 + rows - 1, x, "▁", a | curses.A_DIM)
        except curses.error:
            pass
    if last_val is not None:
        prec = 1 if 0 < abs(last_val) < 10 else 0
        cur = f"{last_val:.{prec}f}{unit}"
        win.addnstr(y0, w - RIGHT, cur.rjust(RIGHT - 1), RIGHT - 1,
                    curses.A_BOLD)
    return


def draw_band_channel(win, y0, rows, samples, color_map):
    """A band/channel track in two rows: at each change the band is
    written on the top row and the channel on the bottom row; the bottom
    row then continues with a rule (───) until the next change. Colored
    per band, so band/channel hops (mesh roaming) pop out at a glance.
      row0  band  (2.4 / 5 / 6), written at each change
      row1  channel number, then a continuation rule until the next change
    """
    win.addnstr(y0, 0, "band".rjust(GUTTER - 2) + " │", GUTTER, curses.A_DIM)
    win.addnstr(y0 + 1, 0, "chan".rjust(GUTTER - 2) + " │", GUTTER,
                curses.A_DIM)

    def keyof(s):
        if not s["connected"] or not s.get("freq"):
            return None
        return (band_of(s["freq"]), freq_to_channel(s["freq"]))

    def put(y, x, ch, attr):
        try:
            win.addstr(y, x, ch, attr)
        except curses.error:
            pass

    prev = None
    label_end = 0                        # column past the current channel label
    for i, s in enumerate(samples):
        x = GUTTER + i
        k = keyof(s)
        if k is None:
            prev = None
            continue
        band, chan = k
        attr = band_color(s["freq"], color_map) or 0
        if k != prev:                    # segment start: write both labels
            for j, c in enumerate(band):
                put(y0, x + j, c, attr | curses.A_BOLD)
            cs = str(chan)
            for j, c in enumerate(cs):
                put(y0 + 1, x + j, c, attr | curses.A_BOLD)
            label_end = x + len(cs)
        elif x >= label_end:             # continuation rule past the label
            put(y0 + 1, x, "─", attr)
        prev = k


def draw_timeline(win, history, color_map):
    """The main panel: stacked multi-row charts, 1 column = 1 second."""
    h, w = win.getmaxyx()
    win.erase()
    win.box()
    n_cols = w - GUTTER - RIGHT
    span = min(len(history), n_cols)
    win.addnstr(0, 2, f" timeline — last {span}s (1 col = 1 s) ",
                w - 4, curses.A_BOLD)
    avail = h - 3                       # minus borders and event row
    if avail < 7 or w < GUTTER + RIGHT + 20:
        win.noutrefresh()
        return
    samples = list(history)[-n_cols:]

    # does the driver give us survey data? (root on some cards)
    have_busy = any(s["busy"] is not None for s in samples[-300:])
    have_noise = any(s["noise"] is not None for s in samples[-300:])

    # ✕ markers on the ping charts: red = the gateway itself was
    # unreachable while associated (the Wi-Fi hop failed); magenta =
    # gateway fine but internet not (problem is past the router).
    def bad_gw(s):
        return color_map["event"] if s.get("gw_loss") else None

    def bad_inet(s):
        if s.get("gw_loss"):
            return color_map["event"]
        if s.get("inet_loss"):
            return color_map["noise"]
        return None

    def chart_aux(y, rows):
        if have_busy:
            draw_chart(win, y, rows, samples, lambda s: s["busy"],
                       0, 100, color_map["busy"], "busy%", "%")
        elif have_noise:
            draw_chart(win, y, rows, samples, lambda s: s["noise"],
                       NOISE_MIN, NOISE_MAX, color_map["noise"], "noise", "")
        else:
            draw_chart(win, y, rows, samples, lambda s: s["beacon"],
                       0, 100, color_map["noise"], "beac%", "%")

    # aux charts in display order; priority decides which survive when
    # the terminal is short (1 = kept longest)
    charts = [
        (4, lambda y, r: draw_chart(       # the Wi-Fi hop, in isolation
            win, y, r, samples, lambda s: s.get("gw_ms"),
            0, 100, color_map["lag"], "router", "ms", bad=bad_gw)),
        (1, lambda y, r: draw_chart(       # the whole path
            win, y, r, samples, lambda s: s.get("inet_ms"),
            0, 500, color_map["lag"], "internet", "ms", bad=bad_inet)),
        (3, lambda y, r: draw_chart(       # our own load on the air
            win, y, r, samples, lambda s: s.get("mbps"),
            0, 30, color_map["rssi"], "traffic", "Mb")),
        (2, lambda y, r: draw_chart(
            win, y, r, samples, lambda s: s.get("retry"),
            0, 100, color_map["busy"], "retry%", "%")),
        (5, chart_aux),
    ]
    # rssi + a 4-row band/channel lane share the top ~30%; the lane is
    # carved out of rssi's height (only when there's room to spare)
    LANE = 2
    rows_top = max(3, int(avail * 0.3))
    rows_lane = LANE if rows_top >= 3 + LANE else 0
    rows_rssi = rows_top - rows_lane
    rest = avail - rows_top
    n_aux = max(1, min(len(charts), rest // 2))
    keep = sorted(p for p, _ in charts)[:n_aux]
    selected = [fn for p, fn in charts if p in keep]

    y = 1
    draw_chart(win, y, rows_rssi, samples, lambda s: s["rssi"],
               RSSI_MIN, RSSI_MAX, color_map["rssi"], "rssi", "",
               disconnect_attr=color_map["event"],
               attr_of=lambda s: band_color(s.get("freq"), color_map))
    y += rows_rssi
    if rows_lane:
        draw_band_channel(win, y, rows_lane, samples, color_map)
        y += rows_lane
    base, extra = divmod(rest, len(selected))
    for i, fn in enumerate(selected):
        r = base + (1 if i < extra else 0)
        fn(y, r)
        y += r
    # event marker row
    win.addnstr(y, 0, "event".rjust(GUTTER - 2) + " │", GUTTER, curses.A_DIM)
    for i, s in enumerate(samples):
        if s["event"]:
            try:
                win.addstr(y, GUTTER + i, "▲",
                           color_map["event"] | curses.A_BOLD)
            except curses.error:
                pass
    win.noutrefresh()


def draw_events(win, events):
    h, w = win.getmaxyx()
    win.erase()
    win.box()
    win.addnstr(0, 2, " events ", w - 4, curses.A_BOLD)
    shown = list(events)[-(h - 2):]
    for i, (ts, kind, detail) in enumerate(shown):
        line = f"{ts.strftime('%H:%M:%S')}  {kind:<11} {detail}"
        try:
            win.addnstr(1 + i, 2, line, w - 4)
        except curses.error:
            pass
    win.noutrefresh()


def main_screen(stdscr, mon, args):
    curses.curs_set(0)
    curses.use_default_colors()
    stdscr.nodelay(True)
    has_color = curses.has_colors()
    if has_color:
        curses.init_pair(1, curses.COLOR_GREEN, -1)   # our AP / rssi
        curses.init_pair(2, curses.COLOR_CYAN, -1)    # other APs
        curses.init_pair(3, curses.COLOR_YELLOW, -1)  # retry / busy
        curses.init_pair(4, curses.COLOR_RED, -1)     # events
        curses.init_pair(5, curses.COLOR_MAGENTA, -1) # noise / beacons
    cp = (lambda n: curses.color_pair(n)) if has_color else (lambda n: 0)
    color_map = {"rssi": cp(1), "noise": cp(5), "busy": cp(3),
                 "event": cp(4), "lag": cp(2)}

    paused = False
    chans_24 = list(range(1, 14))
    chans_5 = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120,
               124, 128, 132, 136, 140, 144, 149, 153, 157, 161, 165]

    while True:
        t0 = time.time()
        sample, aps, last_scan = mon.tick()

        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            break
        if ch in (ord("p"), ord("P")):
            paused = not paused

        if not paused:
            height, width = stdscr.getmaxyx()
            stdscr.erase()
            if height < 24 or width < 80:
                stdscr.addnstr(0, 0, "terminal too small (need >= 80x24) — "
                               "logging continues, q quits", width - 1)
                stdscr.refresh()
            else:
                # status line
                if sample["connected"]:
                    retry_s = (f"{sample['retry']:.0f}%"
                               if sample["retry"] is not None else "?")
                    beac_s = (f"{sample['beacon']:.0f}%"
                              if sample["beacon"] is not None else "?")
                    gw_s = ("LOST" if sample["gw_loss"] else
                            f"{sample['gw_ms']:.0f}ms" if sample["gw_ms"]
                            is not None else "?")
                    inet_s = ("LOST" if sample["inet_loss"] else
                              f"{sample['inet_ms']:.0f}ms" if sample["inet_ms"]
                              is not None else "?")
                    traf_s = (f"↓{sample['rx_mbps']:.1f} ↑{sample['tx_mbps']:.1f}"
                              if sample["rx_mbps"] is not None else "?")
                    node_s = f" {sample['node']}" if sample["node"] else ""
                    status = (f" {mon.iface}  {sample['ssid']}{node_s}  "
                              f"ch {freq_to_channel(sample['freq'])} "
                              f"({band_of(sample['freq'])} GHz)  "
                              f"rssi {sample['rssi']} dBm  "
                              f"retry {retry_s}  beacons {beac_s}  "
                              f"router {gw_s}  internet {inet_s}  "
                              f"{traf_s} Mb/s")
                else:
                    status = f" {mon.iface}  NOT CONNECTED"
                age = time.time() - last_scan if last_scan else None
                status += f"  |  scan {f'{age:.0f}s ago' if age is not None else 'pending'}"
                status += "  |  q quit  p pause"
                attr = cp(4) | curses.A_BOLD if not sample["connected"] else curses.A_REVERSE
                stdscr.addnstr(0, 0, status.ljust(width - 1), width - 1, attr)
                stdscr.noutrefresh()

                # layout: compact spectrum, BIG timeline, small event log
                spec_h = 8 if height < 34 else 10
                ev_h = 4 if height < 30 else 6
                tl_h = height - 1 - spec_h - ev_h
                half = width // 2
                ap_list = sorted(aps.values(), key=lambda a: a["signal"],
                                 reverse=True)
                w24 = [a for a in ap_list if band_of(a["freq"]) == "2.4"]
                w5 = [a for a in ap_list if band_of(a["freq"]) == "5"]

                win24 = curses.newwin(spec_h, half, 1, 0)
                draw_spectrum(win24, f"2.4 GHz — {len(w24)} APs", w24,
                              chans_24, sample.get("bssid"),
                              cp(1) | curses.A_BOLD, cp(2), cp(3))
                win5 = curses.newwin(spec_h, width - half, 1, half)
                draw_spectrum(win5, f"5 GHz — {len(w5)} APs", w5,
                              chans_5, sample.get("bssid"),
                              cp(1) | curses.A_BOLD, cp(2), cp(3))
                wintl = curses.newwin(tl_h, width, 1 + spec_h, 0)
                draw_timeline(wintl, mon.history, color_map)
                if ev_h >= 3:
                    winev = curses.newwin(ev_h, width, 1 + spec_h + tl_h, 0)
                    draw_events(winev, mon.events)
                curses.doupdate()

        # keep a steady 1 Hz cadence
        time.sleep(max(0.0, 1.0 - (time.time() - t0)))


# ---------------------------------------------------------------- fox hunt

BIGFONT = {
    "0": ["████", "█  █", "█  █", "█  █", "████"],
    "1": ["  █ ", " ██ ", "  █ ", "  █ ", " ███"],
    "2": ["████", "   █", "████", "█   ", "████"],
    "3": ["████", "   █", " ███", "   █", "████"],
    "4": ["█  █", "█  █", "████", "   █", "   █"],
    "5": ["████", "█   ", "████", "   █", "████"],
    "6": ["████", "█   ", "████", "█  █", "████"],
    "7": ["████", "   █", "  █ ", " █  ", " █  "],
    "8": ["████", "█  █", "████", "█  █", "████"],
    "9": ["████", "█  █", "████", "   █", "████"],
    "-": ["    ", "    ", "████", "    ", "    "],
    "?": ["████", "   █", "  ██", "    ", "  █ "],
}


def draw_big(win, y, x, text, attr):
    """Render text in a 5-row block font, each pixel 2 columns wide."""
    for row in range(5):
        out = ""
        for chq in text:
            glyph = BIGFONT.get(chq, BIGFONT["?"])
            out += "".join(c * 2 for c in glyph[row]) + "  "
        try:
            win.addstr(y + row, x, out, attr)
        except curses.error:
            pass


def main_track(stdscr, mon, bssid, args):
    """Fox-hunt display: one giant live RSSI readout for a single BSSID.
    Walk toward where the number grows."""
    curses.curs_set(0)
    curses.use_default_colors()
    stdscr.nodelay(True)
    has_color = curses.has_colors()
    if has_color:
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
    cp = (lambda n: curses.color_pair(n)) if has_color else (lambda n: 0)

    history = collections.deque(maxlen=HISTORY_SECONDS)   # rssi or None, 1 Hz
    best = None                     # (rssi, timestr)
    last_reading = None             # (rssi, seen-time)

    while True:
        t0 = time.time()
        mon.tick()                  # keeps CSV logging + events alive
        aps, last_scan, _ = mon.scanner.snapshot()
        ap = aps.get(bssid)

        rssi = None
        if ap and (time.time() - ap["seen"]) < max(8.0, 2 * mon.scanner.interval):
            rssi = ap["signal"]
            last_reading = (rssi, ap["seen"])
            ts = datetime.now().strftime("%H:%M:%S")
            if best is None or rssi > best[0]:
                best = (rssi, ts)
        history.append({"connected": True, "rssi": rssi})

        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            break
        if ch in (ord("r"), ord("R")):
            best = None

        # warmer/colder: average of the last 3 readings vs the 3 before
        valid = [s["rssi"] for s in history if s["rssi"] is not None]
        guide, gattr = "· searching…", curses.A_DIM
        if len(valid) >= 6:
            recent = sum(valid[-3:]) / 3.0
            before = sum(valid[-6:-3]) / 3.0
            if recent - before >= 2:
                guide, gattr = "▲▲ WARMER", cp(1) | curses.A_BOLD
            elif before - recent >= 2:
                guide, gattr = "▼▼ COLDER", cp(4) | curses.A_BOLD
            else:
                guide, gattr = "── steady", cp(3)

        height, width = stdscr.getmaxyx()
        stdscr.erase()
        name = ap["ssid"] if ap else "?"
        chan = freq_to_channel(ap["freq"]) if ap else "?"
        band = band_of(ap["freq"]) if ap else "?"
        stdscr.addnstr(0, 0,
                       f" FOX HUNT  {bssid}  {name}  ch {chan} ({band} GHz)"
                       f"  |  q quit  r reset best ".ljust(width - 1),
                       width - 1, curses.A_REVERSE)

        # the big number
        if rssi is not None:
            text, attr = f"{rssi:.0f}", cp(1) | curses.A_BOLD
            note = "dBm (live)"
        elif last_reading:
            age = time.time() - last_reading[1]
            text, attr = f"{last_reading[0]:.0f}", curses.A_DIM
            note = f"dBm — NOT SEEN for {age:.0f}s (device may be idle)"
        else:
            text, attr = "?", curses.A_DIM
            note = "no sighting yet"
        x = max(0, (width - 10 * len(text)) // 2)
        draw_big(stdscr, 2, x, text, attr)
        stdscr.addnstr(8, max(0, (width - len(note)) // 2), note, width - 1,
                       curses.A_DIM if rssi is None else 0)

        # guidance + best
        stdscr.addnstr(10, max(0, (width - len(guide)) // 2), guide,
                       width - 1, gattr)
        if best:
            b = f"strongest so far: {best[0]:.0f} dBm at {best[1]}"
            stdscr.addnstr(11, max(0, (width - len(b)) // 2), b, width - 1,
                           curses.A_DIM)

        # thermometer: -95 (far) … -20 (touching it)
        t_y = 13
        t_w = max(20, width - 16)
        stdscr.addnstr(t_y, 2, "-95", 4, curses.A_DIM)
        stdscr.addnstr(t_y, width - 5, "-20", 4, curses.A_DIM)
        fill = scale(rssi if rssi is not None else
                     (last_reading[0] if last_reading else None),
                     -95, -20, t_w)
        for i in range(t_w):
            chq, attr2 = "░", curses.A_DIM
            if fill is not None and i <= fill:
                chq = "█"
                attr2 = cp(1) if i < t_w * 0.6 else (cp(3) if i < t_w * 0.85
                                                     else cp(4) | curses.A_BOLD)
            try:
                stdscr.addstr(t_y, 7 + i, chq, attr2)
            except curses.error:
                pass
        if best:
            bpos = scale(best[0], -95, -20, t_w)
            if bpos is not None:
                try:
                    stdscr.addstr(t_y, 7 + bpos, "▲", cp(2) | curses.A_BOLD)
                except curses.error:
                    pass

        # trend: one column per second
        tl_h = height - 16
        if tl_h >= 5:
            wintl = curses.newwin(tl_h, width, 15, 0)
            wintl.erase()
            wintl.box()
            wintl.addnstr(0, 2, " signal trend (1 col = 1 s; gaps = not seen) ",
                          width - 4, curses.A_BOLD)
            draw_chart(wintl, 1, tl_h - 2, list(history)[-(width - GUTTER - RIGHT):],
                       lambda s: s["rssi"], RSSI_MIN, RSSI_MAX, cp(2),
                       "rssi", "")
            wintl.noutrefresh()
        stdscr.noutrefresh()
        curses.doupdate()
        time.sleep(max(0.0, 1.0 - (time.time() - t0)))


# ---------------------------------------------------------------- headless


def headless(mon, seconds, report_every=15):
    """Run the collectors without a UI; print a status line periodically.
    Used for calibration and for long unattended captures."""
    t_end = time.time() + seconds
    last_report = 0
    n = 0
    while time.time() < t_end:
        t0 = time.time()
        sample, aps, last_scan = mon.tick()
        n += 1
        if time.time() - last_report >= report_every:
            last_report = time.time()
            def f(v, fmt="{}"):
                return fmt.format(v) if v is not None else "--"
            print(f"[{sample['ts'].strftime('%H:%M:%S')}] "
                  f"conn={int(sample['connected'])} "
                  f"node={sample['node'] or '--'} "
                  f"rssi={f(sample['rssi'])} "
                  f"tx={f(sample['txrate'])} "
                  f"retry={f(sample['retry'], '{:.0f}%')} "
                  f"beac={f(sample['beacon'], '{:.0f}%')} "
                  f"router={'LOST' if sample['gw_loss'] else f(sample['gw_ms'], '{:.0f}ms')} "
                  f"internet={'LOST' if sample['inet_loss'] else f(sample['inet_ms'], '{:.0f}ms')} "
                  f"traffic={f(sample['mbps'], '{:.1f}Mb')} "
                  f"aps={len(aps)} events={len(mon.events)}", flush=True)
        time.sleep(max(0.0, 1.0 - (time.time() - t0)))
    print(f"done: {n} samples, {len(mon.events)} events")
    for ts, kind, detail in list(mon.events)[-20:]:
        print(f"  {ts.strftime('%H:%M:%S')}  {kind:<11} {detail}")
    print(f"logs: {mon.logs.link_path}")


def debug_once(mon):
    """Collect one sample and print everything as text (no curses)."""
    mon.scanner._scan_once()
    time.sleep(1)
    sample, aps, last_scan = mon.tick()
    print("link:", {k: v for k, v in sample.items() if k != "ts"})
    print(f"scan: {len(aps)} APs (scan age "
          f"{time.time() - last_scan:.0f}s)" if last_scan else "scan: none yet")
    for ap in sorted(aps.values(), key=lambda a: a["signal"], reverse=True):
        print(f"  {ap['signal']:6.1f} dBm  ch {freq_to_channel(ap['freq']):>3} "
              f"({band_of(ap['freq'])} GHz)  {ap['bssid']}  {ap['ssid']}")
    print(f"logs: {mon.logs.link_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--iface", help="wireless interface (default: autodetect)")
    ap.add_argument("--scan-interval", type=float, default=10.0,
                    help="seconds between AP scans (default 10)")
    ap.add_argument("--logdir", default="logs", help="CSV output directory")
    ap.add_argument("--headless", type=float, metavar="SECONDS",
                    help="run without UI for SECONDS (logging only)")
    ap.add_argument("--track", metavar="BSSID",
                    help="fox-hunt mode: giant live signal readout for one "
                         "BSSID — walk toward where the number grows")
    ap.add_argument("--debug-once", action="store_true",
                    help="print one text sample and exit (no UI)")
    args = ap.parse_args()

    if shutil.which("iw") is None:
        sys.exit("wifimon needs 'iw'. Install it with:  sudo apt install iw")

    iface = args.iface or detect_iface()
    if not iface:
        sys.exit("no wireless interface found (try --iface)")

    if args.track:
        args.track = args.track.lower()
        if not re.fullmatch(r"[0-9a-f:]{17}", args.track):
            sys.exit(f"--track wants a BSSID like 10:2c:b1:69:64:ef, "
                     f"got {args.track!r}")
        if args.scan_interval == 10.0:
            args.scan_interval = 3.0    # hunt wants the freshest data it can get

    mon = Monitor(iface, args.scan_interval, args.logdir)
    mon.start()

    if args.debug_once:
        debug_once(mon)
        return
    if args.headless:
        headless(mon, args.headless)
        return

    try:
        if args.track:
            curses.wrapper(main_track, mon, args.track, args)
        else:
            curses.wrapper(main_screen, mon, args)
    except KeyboardInterrupt:
        pass
    print(f"logs written to: {os.path.abspath(args.logdir)}/")


if __name__ == "__main__":
    main()
