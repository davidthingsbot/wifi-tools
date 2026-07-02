#!/usr/bin/env python3
"""
wifianalyze — scorecards and forensics for wifimon captures.

Reads the CSV triples wifimon writes under ./logs/ (link-*, scan-*,
events-*) and produces a text report:

  * overview        — duration, connectivity, channels used, drop count
  * segments        — per (BSSID, channel) stretch: RSSI / retry% / beacon%
  * hourly          — median retry% and beacon% by hour of day
  * disconnects     — the last 60 s of RF before each drop, and its duration
  * suspects        — every intermittent AP ranked by how much worse the
                      air is while it is visible (presence-correlation)
  * events          — event histogram

With several captures, adds a side-by-side comparison table — run one
capture per experiment (channel change, device unplugged, new location)
and let the numbers fight it out.

Usage:
    ./wifianalyze.py                          # newest capture in ./logs
    ./wifianalyze.py 20260702-074007          # by stamp
    ./wifianalyze.py logs/link-...csv ...     # by path, one or many
    ./wifianalyze.py --suspects-top 15 --logdir logs
"""

import argparse
import collections
import csv
import glob
import os
import re
import sys
from datetime import datetime, timedelta


def freq_to_channel(freq):
    freq = int(freq)
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if freq == 2484:
        return 14
    if 5000 < freq < 5925:
        return (freq - 5000) // 5
    if 5955 <= freq <= 7115:
        return (freq - 5950) // 5
    return 0


def med(vals):
    s = sorted(vals)
    return s[len(s) // 2] if s else None


def p90(vals):
    s = sorted(vals)
    return s[int(len(s) * 0.9)] if s else None


def fnum(v, fmt="{:.0f}", none="  --"):
    return fmt.format(v) if v is not None else none


def parse_time(t):
    return datetime.fromisoformat(t)


# ---------------------------------------------------------------- capture


class Capture:
    def __init__(self, stamp, logdir):
        self.stamp = stamp
        self.link = self._read(os.path.join(logdir, f"link-{stamp}.csv"))
        self.scan = self._read(os.path.join(logdir, f"scan-{stamp}.csv"))
        self.events = self._read(os.path.join(logdir, f"events-{stamp}.csv"))
        if not self.link:
            sys.exit(f"no link rows found for capture {stamp}")

    @staticmethod
    def _read(path):
        if not os.path.exists(path):
            return []
        with open(path, newline="") as f:
            return list(csv.DictReader(f))

    # -- helpers -----------------------------------------------------
    def floats(self, col):
        return [float(r[col]) for r in self.link if r.get(col)]

    def duration(self):
        return (parse_time(self.link[-1]["time"])
                - parse_time(self.link[0]["time"]))

    def disconnected_seconds(self):
        return sum(1 for r in self.link if r["connected"] == "0")


# ---------------------------------------------------------------- sections


def section(title):
    print(f"\n══ {title} " + "═" * max(0, 68 - len(title)))


def report_overview(cap):
    section(f"overview — capture {cap.stamp}")
    t0, t1 = cap.link[0]["time"], cap.link[-1]["time"]
    n = len(cap.link)
    disc = cap.disconnected_seconds()
    drops = sum(1 for e in cap.events if e["event"] == "DISCONNECT")
    chans = collections.Counter(
        freq_to_channel(r["freq"]) for r in cap.link if r.get("freq"))
    ssids = sorted({r["ssid"] for r in cap.link if r.get("ssid")})
    print(f"  {t0}  →  {t1}   ({cap.duration()})")
    print(f"  samples: {n}   connected: {100 * (n - disc) / n:.1f}%   "
          f"disconnects: {drops}   time offline: {disc}s")
    print(f"  ssid(s): {', '.join(ssids) or '?'}   "
          f"channels used: {', '.join(str(c) for c in chans)}")
    for col, label, unit in (("rssi_dbm", "rssi", " dBm"),
                             ("retry_pct", "retry", "%"),
                             ("beacon_pct", "beacon", "%")):
        vals = cap.floats(col)
        if vals:
            print(f"  {label:>6}: med={fnum(med(vals))}{unit}  "
                  f"p90={fnum(p90(vals))}{unit}  n={len(vals)}")


def report_segments(cap):
    section("segments (contiguous BSSID+channel stretches)")
    segs = []
    for r in cap.link:
        key = (r["bssid"], r["freq"])
        if not segs or segs[-1]["key"] != key:
            segs.append({"key": key, "rows": []})
        segs[-1]["rows"].append(r)
    print(f"  {'start':>8} {'end':>8}  {'bssid':17} {'ch':>3} {'n':>6}  "
          f"{'rssi':>10}  {'retry%':>11}  {'beac%':>11}")
    for s in segs:
        rs = s["rows"]
        bssid, freq = s["key"]
        if not bssid and len(rs) < 5:
            continue                    # sub-5s glitch
        ch = freq_to_channel(freq) if freq else ""
        rssi = [int(r["rssi_dbm"]) for r in rs if r.get("rssi_dbm")]
        retry = [float(r["retry_pct"]) for r in rs if r.get("retry_pct")]
        beac = [float(r["beacon_pct"]) for r in rs if r.get("beacon_pct")]
        label = bssid or "(disconnected)"
        print(f"  {rs[0]['time'][11:19]:>8} {rs[-1]['time'][11:19]:>8}  "
              f"{label:17} {ch!s:>3} {len(rs):>6}  "
              f"med {fnum(med(rssi)):>4}   "
              f"med {fnum(med(retry)):>3} p90 {fnum(p90(retry)):>3}   "
              f"med {fnum(med(beac)):>3} p90 {fnum(p90(beac)):>3}")


def report_hourly(cap):
    section("hourly medians")
    hours = collections.defaultdict(lambda: {"retry": [], "beac": [], "rssi": []})
    for r in cap.link:
        h = r["time"][11:13]
        if r.get("retry_pct"):
            hours[h]["retry"].append(float(r["retry_pct"]))
        if r.get("beacon_pct"):
            hours[h]["beac"].append(float(r["beacon_pct"]))
        if r.get("rssi_dbm"):
            hours[h]["rssi"].append(int(r["rssi_dbm"]))
    print(f"  {'hour':>5}  {'rssi':>5}  {'retry%':>6}  {'beac%':>5}  "
          f"{'retry% bar':<22}")
    for h in sorted(hours):
        d = hours[h]
        r = med(d["retry"])
        bar = "█" * int((r or 0) / 5)
        print(f"  {h}:00  {fnum(med(d['rssi'])):>5}  {fnum(r):>6}  "
              f"{fnum(med(d['beac'])):>5}  {bar:<22}")


def report_disconnects(cap):
    section("disconnect forensics (the last 60 s before each drop)")
    byt = {r["time"]: r for r in cap.link}
    evs = [e for e in cap.events if e["event"] in ("DISCONNECT", "RECONNECT")]
    if not any(e["event"] == "DISCONNECT" for e in evs):
        print("  no disconnects — good news is also data")
        return
    for i, e in enumerate(evs):
        if e["event"] != "DISCONNECT":
            continue
        t = parse_time(e["time"])
        # how long did the outage last?
        dur = ""
        for e2 in evs[i + 1:]:
            if e2["event"] == "RECONNECT":
                dur = f"  (offline {parse_time(e2['time']) - t})"
                break
        print(f"\n  DROP at {e['time'][11:19]}{dur}")
        print(f"    {'t':>6}  {'rssi':>5}  {'retry%':>6}  {'beac%':>6}")
        for back in range(60, -1, -10):
            r = byt.get((t - timedelta(seconds=back))
                        .isoformat(timespec="seconds"))
            if r:
                print(f"    -{back:>3d}s  {r.get('rssi_dbm') or '--':>5}  "
                      f"{r.get('retry_pct') or '--':>6}  "
                      f"{r.get('beacon_pct') or '--':>6}")


def report_suspects(cap, top):
    section("suspects — air quality vs. each intermittent AP's presence")
    if not cap.scan:
        print("  no scan data in this capture")
        return
    # which minutes was each BSSID visible in?
    seen = collections.defaultdict(set)      # bssid -> minutes
    meta = {}                                # bssid -> ssid/ch/signals
    scan_minutes = set()
    for r in cap.scan:
        mn = r["time"][:16]
        scan_minutes.add(mn)
        seen[r["bssid"]].add(mn)
        m = meta.setdefault(r["bssid"], {"ssid": r["ssid"],
                                         "ch": freq_to_channel(r["freq"]),
                                         "sig": []})
        m["sig"].append(float(r["signal_dbm"]))
    # per-minute retry samples
    minute_retry = collections.defaultdict(list)
    for r in cap.link:
        if r.get("retry_pct") and r["time"][:16] in scan_minutes:
            minute_retry[r["time"][:16]].append(float(r["retry_pct"]))

    rows = []
    for bssid, minutes in seen.items():
        on = [v for mn in minutes for v in minute_retry.get(mn, [])]
        off = [v for mn in scan_minutes - minutes
               for v in minute_retry.get(mn, [])]
        pres = 100 * len(minutes) / len(scan_minutes)
        if len(on) < 120 or len(off) < 120:
            continue                      # need ≥2 min of samples each side
        m = meta[bssid]
        rows.append({
            "bssid": bssid, "ssid": m["ssid"][:20], "ch": m["ch"],
            "pres": pres, "sig_med": med(m["sig"]), "sig_max": max(m["sig"]),
            "on": med(on), "off": med(off), "delta": med(on) - med(off),
        })
    rows.sort(key=lambda r: r["delta"], reverse=True)
    if not rows:
        print("  no AP was intermittent enough to correlate "
              "(need ≥2 min visible AND ≥2 min absent)")
        return
    print(f"  {'bssid':17} {'ssid':20} {'ch':>3} {'seen':>5} "
          f"{'sig med/max':>11}  {'retry% on/off':>13}  {'Δ':>4}")
    for r in rows[:top]:
        notes = []
        if r["sig_max"] < -75:
            notes.append("too weak to matter")
        if r["pres"] < 5 or r["pres"] > 95:
            notes.append("thin evidence")
        note = f"  ({'; '.join(notes)})" if notes else ""
        print(f"  {r['bssid']:17} {r['ssid']:20} {r['ch']:>3} "
              f"{r['pres']:>4.0f}% {r['sig_med']:>5.0f}/{r['sig_max']:>4.0f}  "
              f"{r['on']:>6.0f}/{r['off']:>5.0f}  {r['delta']:>+4.0f}{note}")
    print("  Δ = median retry% while AP visible minus while absent. "
          "Big positive Δ + strong signal + no caveat = suspect.")


def report_events(cap):
    section("event histogram")
    hist = collections.Counter(e["event"] for e in cap.events)
    for k, v in hist.most_common():
        print(f"  {k:<12} {v}")


def report_comparison(caps):
    section("capture comparison")
    print(f"  {'capture':17} {'dur':>9} {'ch':>6} {'rssi':>5} "
          f"{'retry%':>6} {'beac%':>5} {'drops':>5}")
    for cap in caps:
        chans = collections.Counter(freq_to_channel(r["freq"])
                                    for r in cap.link if r.get("freq"))
        ch = "+".join(str(c) for c, _ in chans.most_common(2))
        drops = sum(1 for e in cap.events if e["event"] == "DISCONNECT")
        dur = str(cap.duration()).split(".")[0]
        print(f"  {cap.stamp:17} {dur:>9} {ch:>6} "
              f"{fnum(med(cap.floats('rssi_dbm'))):>5} "
              f"{fnum(med(cap.floats('retry_pct'))):>6} "
              f"{fnum(med(cap.floats('beacon_pct'))):>5} {drops:>5}")


# ---------------------------------------------------------------- main


def resolve_stamps(args):
    if not args.captures:
        links = sorted(glob.glob(os.path.join(args.logdir, "link-*.csv")))
        if not links:
            sys.exit(f"no captures found in {args.logdir}/")
        return [re.search(r"link-(.+)\.csv$", links[-1]).group(1)]
    stamps = []
    for c in args.captures:
        m = re.search(r"(?:link|scan|events)-(.+)\.csv$", c)
        stamps.append(m.group(1) if m else c)
    return stamps


def main():
    ap = argparse.ArgumentParser(
        description="scorecards and forensics for wifimon captures")
    ap.add_argument("captures", nargs="*",
                    help="capture stamp(s) or CSV path(s); default = newest")
    ap.add_argument("--logdir", default="logs")
    ap.add_argument("--suspects-top", type=int, default=10)
    args = ap.parse_args()

    caps = [Capture(s, args.logdir) for s in resolve_stamps(args)]
    for cap in caps:
        report_overview(cap)
        report_segments(cap)
        report_hourly(cap)
        report_disconnects(cap)
        report_suspects(cap, args.suspects_top)
        report_events(cap)
    if len(caps) > 1:
        report_comparison(caps)
    print()


if __name__ == "__main__":
    main()
