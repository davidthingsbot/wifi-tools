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
        rssi    our link signal (dBm); red x = disconnected
        retry%  tx retransmission rate — high = hostile air / contention
        beac%   beacon delivery rate — low = we're missing AP beacons
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
                              "noise_dbm", "busy_pct"])
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
        self.logs = CsvLogs(logdir)
        self.history = collections.deque(maxlen=HISTORY_SECONDS)
        self.events = collections.deque(maxlen=400)
        self.prev = {}                  # previous link sample
        self.rssi_window = collections.deque(maxlen=6)
        self.known_aps = {}             # bssid -> last ap dict (for AP-lost)

    def start(self):
        self.scanner.start()

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

        # ---- event detection
        was = self.prev
        if was:
            if was.get("connected") and not link["connected"]:
                self.event(EVT_DISCONNECT, f"lost {was.get('ssid')} ({was.get('bssid')})")
            elif not was.get("connected") and link["connected"]:
                self.event(EVT_RECONNECT, f"{link.get('ssid')} ch {freq_to_channel(link.get('freq', 0))}")
            elif (link.get("connected") and was.get("bssid")
                  and link.get("bssid") != was.get("bssid")):
                self.event(EVT_ROAM,
                           f"{was.get('bssid')} -> {link.get('bssid')} "
                           f"(ch {freq_to_channel(was.get('freq', 0))} -> "
                           f"{freq_to_channel(link.get('freq', 0))})")
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
        if noise is not None and noise > -80:
            if not self._recent_event(EVT_NOISE, 30):
                self.event(EVT_NOISE, f"noise floor {noise} dBm")
        if busy is not None and busy > 85:
            if not self._recent_event(EVT_BUSY, 30):
                self.event(EVT_BUSY, f"channel {busy:.0f}% busy")

        # ---- scan bookkeeping (AP appearance/disappearance)
        aps, last_scan, fresh = self.scanner.snapshot()
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
            "rssi": link.get("rssi"), "noise": noise, "busy": busy,
            "retry": retry, "beacon": beacon,
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
            nz(noise), nz(busy)])
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


GUTTER = 12                             # left axis gutter width
RIGHT = 7                               # right current-value column


def draw_chart(win, y0, rows, samples, getter, lo, hi, attr,
               label, unit, disconnect_attr=None):
    """A multi-row column chart: one terminal column per sample."""
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
        v = getter(s)
        if disconnect_attr is not None and not s["connected"]:
            try:
                win.addstr(y0 + rows - 1, x, "x", disconnect_attr | curses.A_BOLD)
            except curses.error:
                pass
            continue
        if v is None:
            continue
        last_val = v
        lvl = scale(v, lo, hi, rows * 8)
        full, part = divmod(lvl, 8)
        try:
            for r in range(full):
                win.addstr(y0 + rows - 1 - r, x, "█", attr)
            if part:
                win.addstr(y0 + rows - 1 - full, x, BAR[part - 1], attr)
            elif full == 0:
                win.addstr(y0 + rows - 1, x, "▁", attr | curses.A_DIM)
        except curses.error:
            pass
    if last_val is not None:
        cur = f"{last_val:.0f}{unit}"
        win.addnstr(y0, w - RIGHT, cur.rjust(RIGHT - 1), RIGHT - 1,
                    curses.A_BOLD)
    return


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

    rows_rssi = max(4, int(avail * 0.5))
    rest = avail - rows_rssi
    rows_a = max(2, rest // 2)
    rows_b = rest - rows_a
    y = 1
    draw_chart(win, y, rows_rssi, samples, lambda s: s["rssi"],
               RSSI_MIN, RSSI_MAX, color_map["rssi"], "rssi", "",
               disconnect_attr=color_map["event"])
    y += rows_rssi
    draw_chart(win, y, rows_a, samples, lambda s: s["retry"],
               0, 100, color_map["busy"], "retry%", "%")
    y += rows_a
    if rows_b >= 2:
        if have_busy:
            draw_chart(win, y, rows_b, samples, lambda s: s["busy"],
                       0, 100, color_map["busy"], "busy%", "%")
        elif have_noise:
            draw_chart(win, y, rows_b, samples, lambda s: s["noise"],
                       NOISE_MIN, NOISE_MAX, color_map["noise"], "noise", "")
        else:
            draw_chart(win, y, rows_b, samples, lambda s: s["beacon"],
                       0, 100, color_map["noise"], "beac%", "%")
        y += rows_b
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
    color_map = {"rssi": cp(1), "noise": cp(5), "busy": cp(3), "event": cp(4)}

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
                    status = (f" {mon.iface}  {sample['ssid']}  {sample['bssid']}  "
                              f"ch {freq_to_channel(sample['freq'])} "
                              f"({sample['freq']} MHz)  "
                              f"rssi {sample['rssi']} dBm  "
                              f"tx {sample['txrate'] or '?'} Mb/s  "
                              f"retry {retry_s}  beacons {beac_s}")
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
                  f"rssi={f(sample['rssi'])} "
                  f"tx={f(sample['txrate'])} "
                  f"retry={f(sample['retry'], '{:.0f}%')} "
                  f"beac={f(sample['beacon'], '{:.0f}%')} "
                  f"busy={f(sample['busy'], '{:.0f}%')} "
                  f"noise={f(sample['noise'])} "
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
    ap.add_argument("--debug-once", action="store_true",
                    help="print one text sample and exit (no UI)")
    args = ap.parse_args()

    if shutil.which("iw") is None:
        sys.exit("wifimon needs 'iw'. Install it with:  sudo apt install iw")

    iface = args.iface or detect_iface()
    if not iface:
        sys.exit("no wireless interface found (try --iface)")

    mon = Monitor(iface, args.scan_interval, args.logdir)
    mon.start()

    if args.debug_once:
        debug_once(mon)
        return
    if args.headless:
        headless(mon, args.headless)
        return

    try:
        curses.wrapper(main_screen, mon, args)
    except KeyboardInterrupt:
        pass
    print(f"logs written to: {os.path.abspath(args.logdir)}/")


if __name__ == "__main__":
    main()
