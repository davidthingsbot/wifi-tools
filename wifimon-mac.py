#!/usr/bin/env python3
"""
wifimon-mac — the macOS sibling of wifimon.

Same idea, same screen, same CSV files (wifianalyze.py reads them
unchanged): watch the radio, the air, and the actual internet at 1 Hz,
so when a device drops off Wi-Fi you can see which layer failed.

Has everything the Linux version grew: router/internet ping charts, own
`traffic` chart, and mesh-node identification (which AP you're on: #1,
#2, ...) shown in the status line and roam events.

macOS differences vs the Linux version:
  * noise floor IS available (Mac radios report it) — real `noise` chart
  * retry%/beacon% are NOT available (Apple exposes no station counters);
    the `rate` chart (negotiated tx rate) stands in — rate collapse at
    steady signal means the radio is drowning in retries
  * mesh-node names need a real BSSID, so they require Location Services
    granted to the terminal (else macOS redacts BSSIDs — see --doctor)
  * scans are slower and, without permissions, network names/BSSIDs may
    be hidden by macOS privacy rules (run --doctor for guidance)

Quick start (in Terminal, from this folder):
    python3 wifimon-mac.py --doctor    # checks everything, tells you
                                       # exactly what to click/install
    python3 wifimon-mac.py             # run it (best in a big window)
    sudo python3 wifimon-mac.py        # richest data

Keys: q quit, p pause display (logging continues).
"""

import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import wifimon as base                 # shared charts, pinger, CSV, fonts
except ImportError:
    sys.exit("wifimon-mac.py needs wifimon.py sitting in the same folder.\n"
             "Easiest fix: keep the whole wifi-tools folder together.")

import curses

CoreWLAN = None
try:
    import CoreWLAN as _cw                 # optional: pip3 install pyobjc
    CoreWLAN = _cw
except Exception:
    pass

RATE_MAX = 600                             # Mb/s chart ceiling


# ---------------------------------------------------------------- helpers


def is_macos():
    return sys.platform == "darwin"


def mac_default_gateway():
    out = base.run(["route", "-n", "get", "default"], timeout=5)
    m = re.search(r"gateway:\s*([\d.]+)", out)
    return m.group(1) if m else None


def detect_iface():
    # the Wi-Fi service is en0 on nearly every Mac, but ask to be sure
    out = base.run(["networksetup", "-listallhardwareports"], timeout=10)
    m = re.search(r"Hardware Port: Wi-Fi\nDevice: (\S+)", out)
    if m:
        return m.group(1)
    return "en0"


def synth_freq(chan, band):
    """Fabricate an approximate MHz frequency so the CSVs and channel
    math stay identical to the Linux version."""
    if band.startswith("2"):
        return 2484 if chan == 14 else 2407 + 5 * chan
    if band.startswith("6"):
        return 5950 + 5 * chan
    return 5000 + 5 * chan


# ---------------------------------------------------------------- sources


class SPPoller(threading.Thread):
    """Background `system_profiler SPAirPortDataType` poller — the
    no-permissions-needed source. Slow (~3-5 s per call) but gives the
    current network AND a scan of neighbors, with signal and noise."""

    def __init__(self, iface, interval=12.0):
        super().__init__(daemon=True)
        self.iface = iface
        self.interval = interval
        self.lock = threading.Lock()
        self.current = None                # dict or None
        self.others = []                   # list of ap dicts
        self.updated = 0.0
        self.works = None                  # tri-state: None until first run

    def run(self):
        while True:
            self._poll_once()
            time.sleep(self.interval)

    def _poll_once(self):
        out = base.run(["system_profiler", "SPAirPortDataType", "-json"],
                       timeout=30)
        if not out:
            if self.works is None:
                self.works = False
            return
        try:
            data = json.loads(out)
            ifaces = data["SPAirPortDataType"][0]["spairport_airport_interfaces"]
        except (ValueError, LookupError):
            if self.works is None:
                self.works = False
            return
        me = None
        for entry in ifaces:
            if entry.get("_name") == self.iface:
                me = entry
                break
        me = me or (ifaces[0] if ifaces else None)
        if me is None:
            self.works = False
            return
        cur = self._parse_net(me.get("spairport_current_network_information"))
        others = []
        for n in me.get("spairport_airport_other_local_wireless_networks") or []:
            ap = self._parse_net(n)
            if ap:
                others.append(ap)
        with self.lock:
            self.current = cur
            self.others = others
            self.updated = time.time()
            self.works = True

    @staticmethod
    def _parse_net(n):
        if not n:
            return None
        d = {"ssid": n.get("_name") or "<hidden>"}
        m = re.search(r"(\d+)\s*\((\d+(?:\.\d+)?)\s*GHz",
                      n.get("spairport_network_channel") or "")
        if not m:
            return None
        d["chan"] = int(m.group(1))
        d["band"] = m.group(2)
        d["freq"] = synth_freq(d["chan"], d["band"])
        m = re.search(r"(-\d+)\s*dBm\s*/\s*(-\d+)\s*dBm",
                      n.get("spairport_signal_noise") or "")
        if m:
            d["signal"] = float(m.group(1))
            d["noise"] = int(m.group(2))
        return d if "signal" in d else None

    def snapshot(self):
        with self.lock:
            return self.current, list(self.others), self.updated


class MacLink:
    """1 Hz link state, best source available:
    CoreWLAN (no sudo) > wdutil (sudo) > system_profiler (slow)."""

    def __init__(self, iface, sp):
        self.iface = iface
        self.sp = sp
        self.is_root = os.geteuid() == 0
        self.cw = None
        if CoreWLAN:
            try:
                client = CoreWLAN.CWWiFiClient.sharedWiFiClient()
                self.cw = (client.interfaceWithName_(iface)
                           or client.interface())
            except Exception:
                self.cw = None
        self.redacted = False              # names hidden by macOS privacy

    def poll(self):
        d = {"connected": False}
        out = base.run(["ifconfig", self.iface], timeout=5)
        if "status: active" not in out:
            return d
        d["connected"] = True
        d.update({"ssid": None, "bssid": None, "freq": 0,
                  "rssi": None, "noise": None, "txrate": None})
        if self.cw is not None:
            try:
                rssi = self.cw.rssiValue()
                if rssi:
                    d["rssi"] = int(rssi)
                noise = self.cw.noiseMeasurement()
                if noise:
                    d["noise"] = int(noise)
                rate = self.cw.transmitRate()
                if rate:
                    d["txrate"] = float(rate)
                d["ssid"] = self.cw.ssid()
                d["bssid"] = self.cw.bssid()
                chan = self.cw.wlanChannel()
                if chan is not None:
                    band = {1: "2.4", 2: "5", 3: "6"}.get(
                        chan.channelBand(), "5")
                    d["freq"] = synth_freq(chan.channelNumber(), band)
                self.redacted = d["connected"] and d["ssid"] is None
            except Exception:
                pass
        if d["rssi"] is None and self.is_root:
            self._fill_from_wdutil(d)
        if d["rssi"] is None or d["ssid"] is None:
            self._fill_from_sp(d)
        d["ssid"] = d["ssid"] or "?"
        d["bssid"] = d["bssid"] or "?"
        return d

    def _fill_from_wdutil(self, d):
        out = base.run(["wdutil", "info"], timeout=10)
        for key, pat, conv in (
                ("rssi", r"RSSI\s*:\s*(-\d+)", int),
                ("noise", r"Noise\s*:\s*(-\d+)", int),
                ("txrate", r"Tx Rate\s*:\s*([\d.]+)", float)):
            if d.get(key) is None:
                m = re.search(pat, out)
                if m:
                    d[key] = conv(m.group(1))
        if d.get("ssid") in (None, "<redacted>"):
            m = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
            if m and "redact" not in m.group(1).lower():
                d["ssid"] = m.group(1).strip()
        if d.get("bssid") in (None, "<redacted>"):
            m = re.search(r"BSSID\s*:\s*([0-9a-f:]{17})", out)
            if m:
                d["bssid"] = m.group(1)
        if not d.get("freq"):
            m = re.search(r"Channel\s*:\s*(\d+(?:\.\d+)?)g(\d+)", out)
            if m:
                d["freq"] = synth_freq(int(m.group(2)), m.group(1))

    def _fill_from_sp(self, d):
        cur, _, updated = self.sp.snapshot()
        if not cur or time.time() - updated > 60:
            return
        if d.get("rssi") is None:
            d["rssi"] = int(cur["signal"])
        if d.get("noise") is None:
            d["noise"] = cur.get("noise")
        if d.get("ssid") is None:
            d["ssid"] = cur["ssid"]
        if not d.get("freq"):
            d["freq"] = cur["freq"]


class MacScanner(threading.Thread):
    """Same snapshot() API as the Linux Scanner. Uses CoreWLAN scans when
    names are visible (Location Services granted), otherwise consumes the
    system_profiler poller's neighbor list."""

    def __init__(self, iface, interval, sp, cw_iface):
        super().__init__(daemon=True)
        self.iface = iface
        self.interval = max(interval, 6.0)  # macOS throttles scans anyway
        self.sp = sp
        self.cw = cw_iface
        self.lock = threading.Lock()
        self.aps = {}
        self.last_scan = 0
        self.new_results = False
        self.mode = "?"                    # "corewlan" | "profiler"

    def run(self):
        while True:
            self._scan_once()
            time.sleep(self.interval)

    def _scan_once(self):
        aps = {}
        # when macOS redacts BSSIDs, APs are keyed by SSID~channel — and a
        # mesh (several nodes, same SSID, same channel) would collapse to
        # one entry. Suffix duplicates #2, #3... so every node stays
        # visible. (Field fix from the Mac in the house.)
        seen_keys = collections.Counter()
        now = time.time()
        if self.cw is not None:
            try:
                nets, _err = self.cw.scanForNetworksWithName_error_(None, None)
                for n in (nets or []):
                    ssid = n.ssid()
                    bssid = n.bssid()
                    chan = n.wlanChannel()
                    if chan is None:
                        continue
                    band = {1: "2.4", 2: "5", 3: "6"}.get(chan.channelBand(),
                                                          "5")
                    freq = synth_freq(chan.channelNumber(), band)
                    base_key = (bssid or
                                f"{ssid or '<hidden>'}~ch{chan.channelNumber()}")
                    seen_keys[base_key] += 1
                    key = (base_key if seen_keys[base_key] == 1
                           else f"{base_key}#{seen_keys[base_key]}")
                    aps[key] = {"bssid": key, "ssid": ssid or "<hidden>",
                                "freq": freq, "signal": float(n.rssiValue()),
                                "seen": now}
                if aps and any(a["ssid"] != "<hidden>" for a in aps.values()):
                    self.mode = "corewlan"
            except Exception:
                pass
        if not aps:
            _cur, others, updated = self.sp.snapshot()
            if others and now - updated < 90:
                self.mode = "profiler"
                for ap in others:
                    base_key = f"{ap['ssid']}~ch{ap['chan']}"
                    seen_keys[base_key] += 1
                    key = (base_key if seen_keys[base_key] == 1
                           else f"{base_key}#{seen_keys[base_key]}")
                    aps[key] = {"bssid": key, "ssid": ap["ssid"],
                                "freq": ap["freq"], "signal": ap["signal"],
                                "seen": updated}
                now = updated
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


# ---------------------------------------------------------------- traffic


class MacTraffic:
    """1 Hz own-throughput from `netstat -ibn` (macOS has no /sys). Reads
    the interface's <Link> row (cumulative byte counters) and reports the
    per-second delta in Mb/s, same as the Linux TrafficPoller."""

    def __init__(self, iface):
        self.iface = iface
        self._prev = None               # (time, rx_bytes, tx_bytes)

    def _read(self):
        out = base.run(["netstat", "-ibn"], timeout=5)
        for line in out.splitlines():
            f = line.split()
            # the <Link#n> row carries the true totals; columns are
            # Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes
            if (len(f) >= 10 and f[0] == self.iface
                    and f[2].startswith("<Link")):
                try:
                    return int(f[6]), int(f[9])
                except ValueError:
                    return None
        return None

    def poll(self):
        cur = self._read()
        now = time.time()
        prev = self._prev
        self._prev = (now, cur[0], cur[1]) if cur else None
        if cur is None or prev is None or prev[1] is None:
            return {}
        dt = now - prev[0]
        drx, dtx = cur[0] - prev[1], cur[1] - prev[2]
        if dt <= 0 or drx < 0 or dtx < 0:
            return {}
        return {"rx_mbps": round(drx * 8 / dt / 1e6, 2),
                "tx_mbps": round(dtx * 8 / dt / 1e6, 2)}


# ---------------------------------------------------------------- monitor


class MacMonitor:
    """Same tick() contract as the Linux Monitor, so the fox-hunt screen
    and the CSV/analyzer pipeline work unchanged."""

    def __init__(self, iface, scan_interval, logdir):
        self.iface = iface
        self.sp = SPPoller(iface)
        self.link = MacLink(iface, self.sp)
        self.scanner = MacScanner(iface, scan_interval, self.sp,
                                  self.link.cw)
        self.ping_gw = base.Pinger()
        self.ping_inet = base.Pinger(base.INET_TARGET)
        self.traffic = MacTraffic(iface)
        self.nodemap = base.NodeMap()
        self.logs = base.CsvLogs(logdir)
        self.history = collections.deque(maxlen=base.HISTORY_SECONDS)
        self.events = collections.deque(maxlen=400)
        self.prev = {}
        self.rssi_window = collections.deque(maxlen=6)
        self.rate_window = collections.deque(maxlen=300)
        self.known_aps = {}
        self._gw_bad = 0
        self._inet_bad = 0
        self._lag_high = 0
        self._rate_low = 0
        self._gw_refresh = 0

    def start(self):
        self.sp.start()
        self.scanner.start()
        self.ping_gw.start()
        self.ping_inet.start()

    def event(self, kind, detail=""):
        ts = datetime.now()
        self.events.append((ts, kind, detail))
        self.logs.event_w.writerow([ts.isoformat(timespec="seconds"),
                                    kind, detail])

    def _recent_event(self, kind, seconds):
        now = datetime.now()
        return any(k == kind and (now - t).total_seconds() < seconds
                   for t, k, _ in self.events)

    def tick(self):
        now = datetime.now()
        link = self.link.poll()

        if link.get("connected") and time.time() - self._gw_refresh > 10:
            self.ping_gw.set_target(mac_default_gateway())
            self._gw_refresh = time.time()
        gw_state, gw_ms = self.ping_gw.sample()
        inet_state, inet_ms = self.ping_inet.sample()
        if not link.get("connected"):
            gw_state = inet_state = "warmup"
        gw_loss = gw_state == "loss"
        inet_loss = inet_state == "loss"
        self._gw_bad = self._gw_bad + 1 if gw_loss else 0
        self._inet_bad = self._inet_bad + 1 if inet_loss else 0
        self._lag_high = self._lag_high + 1 if (
            inet_ms is not None and inet_ms > base.LAG_EVENT_MS) else 0
        tput = self.traffic.poll()
        rx_mbps, tx_mbps = tput.get("rx_mbps"), tput.get("tx_mbps")

        # ---- mesh node identity (needs a real, un-redacted BSSID)
        if link.get("connected"):
            self.nodemap.register(link.get("bssid"), link.get("ssid"))

        def node_of(lnk):
            return self.nodemap.label(lnk.get("bssid")) or "?"

        # ---- events
        was = self.prev
        if was:
            if was.get("connected") and not link["connected"]:
                self.event(base.EVT_DISCONNECT,
                           f"lost {was.get('ssid')} {node_of(was)} "
                           f"({was.get('bssid')})")
            elif not was.get("connected") and link["connected"]:
                self.event(base.EVT_RECONNECT,
                           f"{link.get('ssid')} {node_of(link)} ch "
                           f"{base.freq_to_channel(link.get('freq', 0))}")
            elif (link.get("connected")
                  and was.get("bssid") not in (None, "?")
                  and link.get("bssid") not in (None, "?")
                  and link.get("bssid") != was.get("bssid")):
                self.event(base.EVT_ROAM,
                           f"{node_of(was)} ch "
                           f"{base.freq_to_channel(was.get('freq', 0))} -> "
                           f"{node_of(link)} ch "
                           f"{base.freq_to_channel(link.get('freq', 0))} "
                           f"({was.get('bssid')} -> {link.get('bssid')})")
        if link.get("rssi") is not None:
            self.rssi_window.append(link["rssi"])
            if (len(self.rssi_window) == self.rssi_window.maxlen
                    and self.rssi_window[0] - link["rssi"] >= 12):
                self.event(base.EVT_RSSI_DROP,
                           f"{self.rssi_window[0]} -> {link['rssi']} dBm "
                           f"in {self.rssi_window.maxlen}s")
                self.rssi_window.clear()
        noise = link.get("noise")
        if noise is not None and noise > -80:
            if not self._recent_event(base.EVT_NOISE, 30):
                self.event(base.EVT_NOISE, f"noise floor {noise} dBm — "
                           "a non-Wi-Fi transmitter may be active nearby")
        # rate collapse at healthy signal = the macOS stand-in for retry%
        rate = link.get("txrate")
        if rate:
            self.rate_window.append(rate)
            typical = sorted(self.rate_window)[len(self.rate_window) // 2]
            if (len(self.rate_window) > 60 and rate < typical * 0.25
                    and (link.get("rssi") or -99) > -70):
                self._rate_low += 1
            else:
                self._rate_low = 0
            if self._rate_low == 5 and not self._recent_event("RATE COLLAPSE", 60):
                self.event("RATE COLLAPSE",
                           f"tx rate {rate:.0f} Mb/s vs typical "
                           f"{typical:.0f} at rssi {link.get('rssi')} — "
                           "air is hostile (retries)")
        if self._gw_bad == 3 and not self._recent_event(base.EVT_STALL, 30):
            self.event(base.EVT_STALL,
                       f"associated (rssi {link.get('rssi')}) but gateway "
                       f"unreachable — the 'bars lie' moment")
        if (self._inet_bad == 5 and not gw_loss
                and not self._recent_event(base.EVT_INET, 30)):
            self.event(base.EVT_INET, "gateway fine but internet unreachable "
                       "(problem is past the router)")
        if self._lag_high == 5 and not self._recent_event(base.EVT_LAG, 30):
            self.event(base.EVT_LAG, f"internet RTT {inet_ms:.0f} ms sustained")

        # ---- scans + AP-lost debounce (same as Linux)
        aps, last_scan, fresh = self.scanner.snapshot()
        if fresh and link.get("ssid"):
            # every AP broadcasting our SSID (with a real BSSID) is a node
            for ap in aps.values():
                if ap["ssid"] == link["ssid"]:
                    self.nodemap.register(ap["bssid"], link["ssid"])
        if fresh:
            for key, ap in aps.items():
                self.logs.scan_w.writerow([now.isoformat(timespec="seconds"),
                                           key, ap["ssid"], ap["freq"],
                                           base.freq_to_channel(ap["freq"]),
                                           ap["signal"]])
            for key, ap in aps.items():
                self.known_aps[key] = dict(ap, misses=0)
            for key, ap in list(self.known_aps.items()):
                if key in aps:
                    continue
                ap["misses"] = ap.get("misses", 0) + 1
                if ap["misses"] >= 2:
                    if ap["signal"] >= -65:
                        self.event(base.EVT_AP_LOST,
                                   f"{ap['ssid']} ({key}) ch "
                                   f"{base.freq_to_channel(ap['freq'])} "
                                   f"was {ap['signal']:.0f} dBm")
                    del self.known_aps[key]

        # ---- history + CSV (same columns as Linux; retry/beacon empty)
        sample = {
            "ts": now, "connected": link.get("connected", False),
            "node": (self.nodemap.label(link.get("bssid"))
                     if link.get("connected") else None),
            "rssi": link.get("rssi"), "noise": noise, "busy": None,
            "retry": None, "beacon": None,
            "txrate": link.get("txrate"),
            "gw_ms": gw_ms, "inet_ms": inet_ms,
            "gw_loss": gw_loss, "inet_loss": inet_loss,
            "rx_mbps": rx_mbps, "tx_mbps": tx_mbps,
            "mbps": (rx_mbps + tx_mbps
                     if rx_mbps is not None and tx_mbps is not None else None),
            "ssid": link.get("ssid"), "bssid": link.get("bssid"),
            "freq": link.get("freq"),
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
            nz(sample["rssi"]), nz(sample["txrate"]), "", "",
            "", "", "", "",
            nz(noise), "",
            nz(gw_ms), nz(inet_ms), int(gw_loss), int(inet_loss),
            nz(rx_mbps), nz(tx_mbps)])
        self.logs.flush()
        self.prev = link
        return sample, (self.known_aps or aps), last_scan


# ---------------------------------------------------------------- doctor


def doctor(iface):
    """Check every data source and say exactly how to unlock the missing
    ones. Friendly by design — this is the first thing to run."""
    print("wifimon-mac doctor\n" + "=" * 50)
    ok = lambda s: print(f"  ✓ {s}")
    no = lambda s: print(f"  ✗ {s}")
    tip = lambda s: print(f"      → {s}")

    if not is_macos():
        no(f"this is not macOS (platform: {sys.platform})")
        tip("on Linux, use ./wifimon.py instead — it has more data sources")
        return

    ok(f"python {sys.version.split()[0]}")
    ok(f"Wi-Fi interface: {iface}")

    # ping + gateway
    gw = mac_default_gateway()
    if gw:
        ok(f"default gateway found: {gw} (gw/inet charts will work)")
    else:
        no("no default gateway — are you connected to Wi-Fi?")
        tip("join the Wi-Fi network first, then run this again")

    # CoreWLAN
    if CoreWLAN is None:
        no("CoreWLAN python bindings not installed (optional, recommended)")
        tip("fix:  pip3 install pyobjc-framework-CoreWLAN")
        tip("if pip3 is missing:  python3 -m ensurepip --user  and retry")
        tip("without it, wifimon-mac still works via slower fallbacks")
    else:
        try:
            cw = CoreWLAN.CWWiFiClient.sharedWiFiClient().interface()
            rssi = cw.rssiValue() if cw else 0
            if rssi:
                ok(f"CoreWLAN live: rssi {rssi} dBm, noise "
                   f"{cw.noiseMeasurement()} dBm, tx {cw.transmitRate():.0f} Mb/s")
            else:
                no("CoreWLAN loaded but returned no signal (not connected?)")
            if cw and cw.ssid() is None and rssi:
                no("macOS is hiding network names (privacy rule)")
                tip("fix: System Settings → Privacy & Security → Location")
                tip("Services → scroll to your terminal app (Terminal or")
                tip("iTerm) → turn it ON → quit and reopen the terminal")
                tip("(names stay hidden until the terminal restarts)")
            elif cw and cw.ssid():
                ok(f"network names visible (connected to: {cw.ssid()})")
        except Exception as e:
            no(f"CoreWLAN error: {e}")

    # wdutil
    if os.geteuid() == 0:
        out = base.run(["wdutil", "info"], timeout=10)
        if "RSSI" in out:
            ok("wdutil works (running as root — richest data)")
        else:
            no("wdutil gave no data")
    else:
        print("  • not running as root — that's fine, but:")
        tip("sudo python3 wifimon-mac.py   unlocks wdutil (fullest link info)")

    # system_profiler
    print("  • checking system_profiler (takes a few seconds)...")
    sp = SPPoller(iface)
    sp._poll_once()
    cur, others, _ = sp.snapshot()
    if sp.works:
        ok(f"system_profiler works: {len(others)} neighbor networks visible"
           + (f", connected to {cur['ssid']}" if cur else ""))
    else:
        no("system_profiler returned nothing useful")
        tip("this is unusual — try:  system_profiler SPAirPortDataType")

    print("\nverdict:")
    if gw and (CoreWLAN or os.geteuid() == 0 or sp.works):
        print("  ready to go:  python3 wifimon-mac.py")
    else:
        print("  fix the ✗ items above, then run --doctor again")


# ---------------------------------------------------------------- UI


def draw_timeline_mac(win, history, color_map):
    h, w = win.getmaxyx()
    win.erase()
    win.box()
    n_cols = w - base.GUTTER - base.RIGHT
    span = min(len(history), n_cols)
    win.addnstr(0, 2, f" timeline — last {span}s (1 col = 1 s) ",
                w - 4, curses.A_BOLD)
    avail = h - 3
    if avail < 7 or w < base.GUTTER + base.RIGHT + 20:
        win.noutrefresh()
        return
    samples = list(history)[-n_cols:]

    def bad_gw(s):
        return color_map["event"] if s.get("gw_loss") else None

    def bad_inet(s):
        if s.get("gw_loss"):
            return color_map["event"]
        if s.get("inet_loss"):
            return color_map["noise"]
        return None

    charts = [                             # (priority, draw)
        (2, lambda y, r: base.draw_chart(
            win, y, r, samples, lambda s: s.get("gw_ms"),
            0, 100, color_map["lag"], "router", "ms", bad=bad_gw)),
        (1, lambda y, r: base.draw_chart(
            win, y, r, samples, lambda s: s.get("inet_ms"),
            0, 500, color_map["lag"], "internet", "ms", bad=bad_inet)),
        (4, lambda y, r: base.draw_chart(
            win, y, r, samples, lambda s: s.get("mbps"),
            0, 30, color_map["rssi"], "traffic", "Mb")),
        (3, lambda y, r: base.draw_chart(
            win, y, r, samples, lambda s: s.get("txrate"),
            0, RATE_MAX, color_map["busy"], "rate", "Mb")),
        (5, lambda y, r: base.draw_chart(
            win, y, r, samples, lambda s: s.get("noise"),
            base.NOISE_MIN, base.NOISE_MAX, color_map["noise"],
            "noise", "")),
    ]
    # rssi + a 4-row band/channel lane share the top; the lane is carved
    # out of rssi's height (only when there's room to spare)
    LANE = 2
    rows_top = max(3, int(avail * 0.3))
    rows_lane = LANE if rows_top >= 3 + LANE else 0
    rows_rssi = rows_top - rows_lane
    rest = avail - rows_top
    n_aux = max(1, min(len(charts), rest // 2))
    keep = sorted(p for p, _ in charts)[:n_aux]
    selected = [fn for p, fn in charts if p in keep]

    y = 1
    base.draw_chart(win, y, rows_rssi, samples, lambda s: s["rssi"],
                    base.RSSI_MIN, base.RSSI_MAX, color_map["rssi"],
                    "rssi", "", disconnect_attr=color_map["event"],
                    attr_of=lambda s: base.band_color(s.get("freq"), color_map))
    y += rows_rssi
    if rows_lane:
        base.draw_band_channel(win, y, rows_lane, samples, color_map)
        y += rows_lane
    per, extra = divmod(rest, len(selected))
    for i, fn in enumerate(selected):
        r = per + (1 if i < extra else 0)
        fn(y, r)
        y += r
    win.addnstr(y, 0, "event".rjust(base.GUTTER - 2) + " │",
                base.GUTTER, curses.A_DIM)
    for i, s in enumerate(samples):
        if s["event"]:
            try:
                win.addstr(y, base.GUTTER + i, "▲",
                           color_map["event"] | curses.A_BOLD)
            except curses.error:
                pass
    win.noutrefresh()


def main_screen(stdscr, mon, args):
    curses.curs_set(0)
    curses.use_default_colors()
    stdscr.nodelay(True)
    has_color = curses.has_colors()
    if has_color:
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
        curses.init_pair(5, curses.COLOR_MAGENTA, -1)
    cp = (lambda n: curses.color_pair(n)) if has_color else (lambda n: 0)
    color_map = {"rssi": cp(1), "noise": cp(5), "busy": cp(3),
                 "event": cp(4), "lag": cp(2),
                 "band": base.init_band_colors()}
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
                stdscr.addnstr(0, 0, "window too small — make the Terminal "
                               "window bigger (logging continues; q quits)",
                               width - 1)
                stdscr.refresh()
            else:
                if sample["connected"]:
                    def f(v, fmt="{}"):
                        return fmt.format(v) if v is not None else "?"
                    gw_s = ("LOST" if sample["gw_loss"] else
                            f(sample["gw_ms"], "{:.0f}ms"))
                    inet_s = ("LOST" if sample["inet_loss"] else
                              f(sample["inet_ms"], "{:.0f}ms"))
                    traf_s = (f"↓{sample['rx_mbps']:.1f} ↑{sample['tx_mbps']:.1f}"
                              if sample["rx_mbps"] is not None else "?")
                    node_s = f" {sample['node']}" if sample["node"] else ""
                    status = (f" {mon.iface}  {sample['ssid']}{node_s}  "
                              f"ch {base.freq_to_channel(sample['freq'])}  "
                              f"rssi {f(sample['rssi'])} dBm  "
                              f"noise {f(sample['noise'])} dBm  "
                              f"tx {f(sample['txrate'], '{:.0f}')} Mb/s  "
                              f"router {gw_s}  internet {inet_s}  "
                              f"{traf_s} Mb/s")
                    if mon.link.redacted:
                        status += "  [names/node hidden: see --doctor]"
                else:
                    status = f" {mon.iface}  NOT CONNECTED"
                age = time.time() - last_scan if last_scan else None
                status += (f"  |  scan {age:.0f}s ago ({mon.scanner.mode})"
                           if age is not None else "  |  scan pending")
                status += "  |  q quit"
                attr = (cp(4) | curses.A_BOLD if not sample["connected"]
                        else curses.A_REVERSE)
                stdscr.addnstr(0, 0, status.ljust(width - 1), width - 1, attr)
                stdscr.noutrefresh()

                spec_h = 8 if height < 34 else 10
                ev_h = 4 if height < 30 else 6
                tl_h = height - 1 - spec_h - ev_h
                half = width // 2
                ap_list = sorted(aps.values(), key=lambda a: a["signal"],
                                 reverse=True)
                w24 = [a for a in ap_list if base.band_of(a["freq"]) == "2.4"]
                w5 = [a for a in ap_list if base.band_of(a["freq"]) == "5"]
                b24 = color_map["band"].get("2.4") or cp(1)
                b5 = color_map["band"].get("5") or cp(1)
                win24 = curses.newwin(spec_h, half, 1, 0)
                base.draw_spectrum(win24, f"2.4 GHz — {len(w24)} APs", w24,
                                   chans_24, sample.get("bssid"),
                                   b24 | curses.A_BOLD, b24 | curses.A_DIM,
                                   cp(3))
                win5 = curses.newwin(spec_h, width - half, 1, half)
                base.draw_spectrum(win5, f"5 GHz — {len(w5)} APs", w5,
                                   chans_5, sample.get("bssid"),
                                   b5 | curses.A_BOLD, b5 | curses.A_DIM,
                                   cp(3))
                wintl = curses.newwin(tl_h, width, 1 + spec_h, 0)
                draw_timeline_mac(wintl, mon.history, color_map)
                if ev_h >= 3:
                    winev = curses.newwin(ev_h, width, 1 + spec_h + tl_h, 0)
                    base.draw_events(winev, mon.events)
                curses.doupdate()
        time.sleep(max(0.0, 1.0 - (time.time() - t0)))


def headless(mon, seconds, report_every=15):
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
                  f"rssi={f(sample['rssi'])} noise={f(sample['noise'])} "
                  f"tx={f(sample['txrate'], '{:.0f}')} "
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
    print("collecting one sample (the first scan can take ~15 s)...")
    mon.sp._poll_once()
    mon.scanner._scan_once()
    time.sleep(1)
    sample, aps, last_scan = mon.tick()
    print("link:", {k: v for k, v in sample.items() if k != "ts"})
    print(f"scan mode: {mon.scanner.mode}, {len(aps)} APs")
    for ap in sorted(aps.values(), key=lambda a: a["signal"], reverse=True):
        print(f"  {ap['signal']:6.1f} dBm  ch "
              f"{base.freq_to_channel(ap['freq']):>3} "
              f"({base.band_of(ap['freq'])} GHz)  {ap['bssid']}  {ap['ssid']}")
    print(f"logs: {mon.logs.link_path}")
    print("\nif anything above looks wrong or empty, run:  "
          "python3 wifimon-mac.py --doctor")


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(
        description="macOS Wi-Fi RF monitor (sibling of wifimon.py)")
    ap.add_argument("--iface", help="Wi-Fi interface (default: autodetect)")
    ap.add_argument("--scan-interval", type=float, default=10.0)
    ap.add_argument("--logdir", default="logs")
    ap.add_argument("--headless", type=float, metavar="SECONDS")
    ap.add_argument("--track", metavar="BSSID_OR_SSID",
                    help="fox-hunt mode: giant live signal readout; give a "
                         "BSSID (aa:bb:...) or a network name")
    ap.add_argument("--debug-once", action="store_true")
    ap.add_argument("--doctor", action="store_true",
                    help="check permissions/data sources and explain fixes")
    args = ap.parse_args()

    iface = args.iface or detect_iface()

    if args.doctor:
        doctor(iface)
        return

    if not is_macos():
        sys.exit(f"this is the macOS version (platform here: {sys.platform})"
                 " — on Linux use ./wifimon.py")

    # friendly preflight: say what we can see, and how to see more
    tips = []
    if CoreWLAN is None:
        tips.append("more detail available:  pip3 install "
                    "pyobjc-framework-CoreWLAN   (then rerun)")
    if os.geteuid() != 0:
        tips.append("richest link data:      sudo python3 wifimon-mac.py")
    if tips:
        print("wifimon-mac starting (works as-is; optional upgrades):")
        for t in tips:
            print(f"  • {t}")
        print("full checkup any time:    python3 wifimon-mac.py --doctor")
        time.sleep(2)

    mon = MacMonitor(iface, args.scan_interval, args.logdir)
    mon.start()

    if args.debug_once:
        debug_once(mon)
        return
    if args.headless:
        headless(mon, args.headless)
        return

    if args.track:
        target = args.track.lower()
        print(f"waiting for a first sighting of {args.track!r} "
              "(scans take ~10-15 s)...")
        key = None
        for _ in range(12):
            time.sleep(5)
            aps, _, _ = mon.scanner.snapshot()
            for k, ap2 in aps.items():
                if k.lower() == target or ap2["ssid"].lower() == target:
                    key = k
                    break
            if key:
                break
        if not key:
            sys.exit(f"never saw {args.track!r} in a scan. Check the name, "
                     "or run --debug-once to list everything visible. "
                     "(If all networks show as <hidden>, run --doctor — "
                     "macOS is hiding names until you grant Location "
                     "Services to the terminal.)")
        print(f"found it: {key} — starting the hunt")
        try:
            curses.wrapper(base.main_track, mon, key, args)
        except KeyboardInterrupt:
            pass
    else:
        try:
            curses.wrapper(main_screen, mon, args)
        except KeyboardInterrupt:
            pass
    print(f"logs written to: {os.path.abspath(args.logdir)}/")


if __name__ == "__main__":
    main()
