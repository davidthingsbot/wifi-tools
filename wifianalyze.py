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
import bisect
import collections
import csv
import glob
import math
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


def loudness(sig_max):
    """How loud this AP gets at the capture position, in words."""
    if sig_max >= -45:
        return "BLASTING"
    if sig_max >= -60:
        return "loud"
    if sig_max >= -72:
        return "moderate"
    if sig_max >= -82:
        return "weak"
    return "faint"


def suspect_stats(on, off):
    """Association strength + confidence between an AP's visibility and
    bad air, computed on per-minute retry medians.

    assoc = probability of superiority: the chance that a randomly chosen
    visible-minute has worse retry% than a randomly chosen absent-minute.
    50% = unrelated, 100% = visible-minutes are always worse.

    conf = how unlikely this association is to be luck, given how many
    minutes we observed (a Mann-Whitney z-score, reported in sigmas).
    """
    off_sorted = sorted(off)
    wins = 0.0
    for v in on:
        lo = bisect.bisect_left(off_sorted, v)
        hi = bisect.bisect_right(off_sorted, v)
        wins += lo + (hi - lo) / 2.0
    n1, n2 = len(on), len(off)
    ps = wins / (n1 * n2)
    z = (wins - n1 * n2 / 2.0) / math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12.0)
    return ps, z


def report_suspects(cap, top, show_all=False):
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
    # one retry number per minute (the median), so that a long capture
    # doesn't count each second as independent evidence
    per_min = collections.defaultdict(list)
    for r in cap.link:
        if r.get("retry_pct") and r["time"][:16] in scan_minutes:
            per_min[r["time"][:16]].append(float(r["retry_pct"]))
    minute_retry = {mn: med(v) for mn, v in per_min.items()}

    rows = []
    for bssid, minutes in seen.items():
        on = [minute_retry[mn] for mn in minutes if mn in minute_retry]
        off = [minute_retry[mn] for mn in scan_minutes - minutes
               if mn in minute_retry]
        if len(on) < 10 or len(off) < 10:
            continue                      # need ≥10 minutes on each side
        ps, z = suspect_stats(on, off)
        m = meta[bssid]
        sig_max = max(m["sig"])
        rows.append({
            "bssid": bssid, "ssid": m["ssid"][:18], "ch": m["ch"],
            "pres": 100 * len(on) / (len(on) + len(off)),
            "sig_med": med(m["sig"]), "sig_max": sig_max,
            "loud": loudness(sig_max),
            "on": med(on), "off": med(off),
            "assoc": 100 * ps, "z": z,
            "credible": (ps >= 0.60 and z >= 2.0 and sig_max >= -72),
        })
    if not rows:
        print("  no AP was intermittent enough to correlate "
              "(need ≥10 minutes visible AND ≥10 minutes absent)")
        return
    rows.sort(key=lambda r: (not r["credible"], -r["assoc"]))

    def emit(rs):
        print(f"  {'bssid':17} {'ssid':18} {'ch':>3} {'seen':>5} "
              f"{'heard at':>13}  {'retry% on/off':>13} {'assoc':>6} {'conf':>5}")
        for r in rs:
            print(f"  {r['bssid']:17} {r['ssid']:18} {r['ch']:>3} "
                  f"{r['pres']:>4.0f}% {r['loud']:>8} {r['sig_max']:>4.0f}  "
                  f"{r['on']:>6.0f}/{r['off']:>5.0f} {r['assoc']:>5.0f}% "
                  f"{min(r['z'], 9.9):>4.1f}σ")

    credible = [r for r in rows if r["credible"]]
    # anything this loud is physically close to the capture position and
    # worth investigating no matter what the correlation says — a bursty
    # transmitter's *beacon visibility* badly understates its activity
    extreme = [r for r in rows if r["sig_max"] >= -50 and not r["credible"]]
    others = [r for r in rows if not r["credible"] and r not in extreme]
    if credible:
        print("  ── likely suspects (associated + confident + loud enough "
              "to be physical) ──")
        emit(credible[:top])
    else:
        print("  no credible suspects — nothing both audible and reliably "
              "associated with bad air")
    if extreme:
        print("\n  ── extreme transmitters at this position — investigate "
              "regardless of assoc ──")
        emit(extreme[:top])
    if others and (show_all or not credible):
        print("\n  ── correlated but NOT credible (weak signal or could be "
              "chance — context only) ──")
        emit(others[:top if show_all else 3])
    print("""
  how to read this:
    assoc  chance that a random minute with this AP visible has worse
           retry% than a random minute without it. 50% = unrelated,
           60% = mild, 75% = strong, 90%+ = near-lockstep.
    conf   how unlikely that association is to be luck, in sigmas (σ),
           given the number of minutes observed. <2σ could easily be
           chance; ≥4σ is solid.
    heard at  the AP's strongest beacon at this position (word + dBm). A
           weak/faint AP is rarely the physical cause even when assoc is
           high — that combination usually means shared timing (it and
           the real interferer are both active in the evening). Its
           clients might still be near you, so demote it, don't erase it.
    A high number in ONE column means nothing; a suspect needs all three.
    Exception: anything heard at -50 dBm or louder is *in the room with
    you* and gets listed regardless — bursty devices (cameras, hubs) can
    wreck the air while beaconing too rarely for assoc to catch them.
    This is correlation: use it to pick the next unplug-and-measure
    experiment, not to convict.""")


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
    ap.add_argument("--suspects-all", action="store_true",
                    help="also list the non-credible correlations in full")
    args = ap.parse_args()

    caps = [Capture(s, args.logdir) for s in resolve_stamps(args)]
    for cap in caps:
        report_overview(cap)
        report_segments(cap)
        report_hourly(cap)
        report_disconnects(cap)
        report_suspects(cap, args.suspects_top, args.suspects_all)
        report_events(cap)
    if len(caps) > 1:
        report_comparison(caps)
    print()


if __name__ == "__main__":
    main()
