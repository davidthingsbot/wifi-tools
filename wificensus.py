#!/usr/bin/env python3
"""
wificensus — monitor-mode airtime census for Linux.

Answers "who is actually using the air on this channel?" — every device
transmitting on the AP's channel, not just this laptop. For each station:
frame count, bytes, retry rate, estimated airtime %, signal, and the
BSSID it's talking to.

This is the passive-sniff sibling of wifimon. What it CAN see: 802.11
headers and radiotap metadata for every frame in the air — transmitter,
receiver, type, size, retry flag, data rate, per-frame RSSI. What it
CANNOT see: frame contents (WPA2/WPA3 encrypts the payload) — which does
not matter for congestion diagnosis.

Five things to know:
  * NOT ALL CARDS CAN DO THIS. Monitor mode has to be supported by the
    driver, and Intel iwlwifi cards (very common in laptops) usually
    can NOT deliver monitor frames even though they advertise the mode.
    The tool checks up front and, if it captures nothing, tells you so
    and points you at a cheap USB adapter that works. It is not a bug in
    the tool — it is the radio.
  * monitor mode DROPS this machine's Wi-Fi connection while running
    (a single radio can't be associated and sniffing at once). Run it in
    bursts; the connection restores on exit.
  * it listens to ONE channel at a time (point it at the AP's channel).
  * it needs sudo (monitor mode + raw socket).
  * airtime is an ESTIMATE (frame bytes / data rate) — good for ranking
    who dominates the channel, not an exact duty cycle.

Usage:
    sudo ./wificensus.py --channel 11              # 2.4 GHz ch 11
    sudo ./wificensus.py --channel 149 --seconds 60
    sudo ./wificensus.py --channel 6 --iface wlp0s20f3 --logdir logs

Keys: q quit, s cycle sort (airtime / frames / retries / signal).
"""

import argparse
import collections
import csv
import curses
import os
import re
import socket
import struct
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------- radiotap

# (bit -> (size_bytes, align)) for the fields we parse; others skipped
RT_FIELDS = {
    0: (8, 8),   # TSFT
    1: (1, 1),   # Flags
    2: (1, 1),   # Rate (legacy, 500 kbps units)
    3: (4, 2),   # Channel (freq + flags)
    4: (2, 2),   # FHSS
    5: (1, 1),   # dBm antenna signal (signed)
    6: (1, 1),   # dBm antenna noise (signed)
    7: (2, 2),   # lock quality
    8: (2, 2),   # tx attenuation
    9: (2, 2),   # dB tx attenuation
    10: (1, 1),  # dBm tx power
    11: (1, 1),  # antenna
    12: (1, 1),  # dB antenna signal
    13: (1, 1),  # dB antenna noise
    14: (2, 2),  # RX flags
    15: (2, 2),  # TX flags
    16: (1, 1),  # RTS retries
    17: (1, 1),  # data retries
    18: (4, 4),  # XChannel
    19: (3, 1),  # MCS (known, flags, index)
    20: (8, 4),  # A-MPDU status
    21: (12, 2),  # VHT
    22: (12, 4),  # timestamp
    23: (12, 2),  # HE
    24: (12, 2),  # HE-MU
}

RT_FLAG_FCS = 0x10           # Flags field: frame includes trailing FCS
RT_FLAG_BADFCS = 0x40        # Flags field: failed FCS check

# HT MCS -> Mbps, [20MHz long GI, 20 short, 40 long, 40 short], single stream.
# For NSS>1 we multiply by the stream count (index // 8) + 1.
HT_BASE = [
    (6.5, 7.2, 13.5, 15.0), (13.0, 14.4, 27.0, 30.0),
    (19.5, 21.7, 40.5, 45.0), (26.0, 28.9, 54.0, 60.0),
    (39.0, 43.3, 81.0, 90.0), (52.0, 57.8, 108.0, 120.0),
    (58.5, 65.0, 121.5, 135.0), (65.0, 72.2, 135.0, 150.0),
]


def align(offset, a):
    return (offset + a - 1) & ~(a - 1)


def parse_radiotap(buf):
    """Return (header_len, info dict) or (None, None) if malformed.
    info may contain: rate_mbps, signal, flags, mcs (index,bw,sgi)."""
    if len(buf) < 8:
        return None, None
    version, _pad, length = struct.unpack_from("<BBH", buf, 0)
    if version != 0 or length < 8 or length > len(buf):
        return None, None
    # collect present bitmaps (extended if bit 31 set)
    presents = []
    off = 4
    while True:
        (word,) = struct.unpack_from("<I", buf, off)
        presents.append(word)
        off += 4
        if not (word & (1 << 31)):
            break
        if off + 4 > length:
            return None, None
    info = {}
    pos = off
    # iterate the standard fields in bit order of the first bitmap only;
    # that covers everything through the HE fields we care about
    for bit in range(0, 25):
        word_idx = bit // 32
        if word_idx >= len(presents):
            break
        if not (presents[word_idx] & (1 << (bit % 32))):
            continue
        if bit not in RT_FIELDS:
            continue
        size, a = RT_FIELDS[bit]
        pos = align(pos, a)
        if pos + size > length:
            break
        if bit == 1:
            info["flags"] = buf[pos]
        elif bit == 2:
            info["rate_mbps"] = buf[pos] * 0.5
        elif bit == 5:
            (sig,) = struct.unpack_from("<b", buf, pos)
            info["signal"] = sig
        elif bit == 19:
            known, mflags, index = buf[pos], buf[pos + 1], buf[pos + 2]
            bw40 = (mflags & 0x03) == 1 if (known & 0x02) else False
            sgi = bool(mflags & 0x04) if (known & 0x04) else False
            info["mcs"] = (index, bw40, sgi)
        pos += size
    return length, info


def rate_from_info(info):
    """Best data-rate estimate in Mbps."""
    if "rate_mbps" in info and info["rate_mbps"] > 0:
        return info["rate_mbps"]
    if "mcs" in info:
        index, bw40, sgi = info["mcs"]
        streams, base = divmod(index, 8)
        if base < len(HT_BASE):
            col = (2 if bw40 else 0) + (1 if sgi else 0)
            return HT_BASE[base][col] * (streams + 1)
    return 65.0                  # reasonable modern default when unknown


# ---------------------------------------------------------------- 802.11

FT_MGMT, FT_CTRL, FT_DATA = 0, 1, 2
# control subtypes that DO carry a transmitter address (addr2)
CTRL_HAS_TA = {0x8, 0x9, 0xa, 0xb}   # BlockAckReq, BlockAck, PS-Poll, RTS


def parse_dot11(buf):
    """Parse the 802.11 header. Return dict or None.
    Keys: ftype, subtype, retry, ra, ta (may be None), bssid (may be None),
    protected."""
    if len(buf) < 2:
        return None
    fc0, fc1 = buf[0], buf[1]
    ftype = (fc0 >> 2) & 0x3
    subtype = (fc0 >> 4) & 0xf
    to_ds = fc1 & 0x01
    from_ds = (fc1 >> 1) & 0x01
    d = {"ftype": ftype, "subtype": subtype,
         "retry": bool(fc1 & 0x08), "protected": bool(fc1 & 0x40),
         "ra": None, "ta": None, "bssid": None}

    def mac(o):
        if o + 6 > len(buf):
            return None
        return ":".join(f"{b:02x}" for b in buf[o:o + 6])

    if len(buf) >= 10:
        d["ra"] = mac(4)                 # addr1 = receiver, always present
    if ftype == FT_CTRL:
        if subtype in CTRL_HAS_TA and len(buf) >= 16:
            d["ta"] = mac(10)
        return d
    # mgmt & data: addr2 = transmitter, addr3 = usually BSSID
    if len(buf) >= 16:
        d["ta"] = mac(10)
    if len(buf) >= 22:
        addr3 = mac(16)
        if ftype == FT_MGMT:
            d["bssid"] = addr3
        elif not to_ds and not from_ds:
            d["bssid"] = addr3
        elif to_ds and not from_ds:
            d["bssid"] = d["ra"]          # addr1 is BSSID (frame to AP)
        elif from_ds and not to_ds:
            d["bssid"] = d["ta"]          # addr2 is BSSID (frame from AP)
    return d


# ---------------------------------------------------------------- oui


def load_oui():
    """Best-effort local OUI->vendor map from files that ship with common
    tools; returns {} if none found (we just show MACs then)."""
    for path in ("/usr/share/ieee-data/oui.txt",
                 "/var/lib/ieee-data/oui.txt",
                 "/usr/share/nmap/nmap-mac-prefixes",
                 "/usr/share/wireshark/manuf"):
        if not os.path.exists(path):
            continue
        table = {}
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = re.match(r"^([0-9A-Fa-f]{2}[:-]?[0-9A-Fa-f]{2}[:-]?"
                                 r"[0-9A-Fa-f]{2})\s+(.+)", line)
                    if m:
                        pre = m.group(1).replace(":", "").replace("-", "")
                        table[pre.upper()] = m.group(2).strip()[:18]
            if table:
                return table
        except OSError:
            continue
    return {}


def vendor_of(mac, oui):
    if not oui or not mac:
        return ""
    return oui.get(mac.replace(":", "").upper()[:6], "")


# ---------------------------------------------------------------- interface


def sh(cmd):
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return r.returncode == 0, (r.stdout + r.stderr)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def detect_iface():
    ok, out = sh(["iw", "dev"])
    m = re.findall(r"Interface\s+(\S+)", out)
    return m[0] if m else None


def chan_to_freq(ch):
    if 1 <= ch <= 13:
        return 2407 + 5 * ch
    if ch == 14:
        return 2484
    return 5000 + 5 * ch


def phy_supports_monitor_combo():
    """True if the card lists 'monitor' in a valid interface combination.
    Many Intel iwlwifi cards advertise monitor as a *mode* but omit it from
    every *combination* — meaning it won't actually deliver frames."""
    ok, out = sh(["iw", "phy"])
    m = re.search(r"valid interface combinations:(.+?)(?:\n\t[A-Z]|\Z)",
                  out, re.S)
    return bool(m and "monitor" in m.group(1))


class MonitorMode:
    """Context manager: put the interface into monitor mode on a channel,
    restore managed mode (and let NetworkManager reconnect) on exit.

    Tells NetworkManager to stop managing the interface first — otherwise
    NM reclaims it mid-capture and silently reverts it to managed."""

    def __init__(self, iface, channel):
        self.iface = iface
        self.channel = channel
        self._nm_released = False

    def __enter__(self):
        # stop NetworkManager fighting us for the interface
        if shutil.which("nmcli"):
            ok, _ = sh(["nmcli", "device", "set", self.iface, "managed", "no"])
            self._nm_released = ok
        steps = [
            ["ip", "link", "set", self.iface, "down"],
            ["iw", "dev", self.iface, "set", "type", "monitor"],
            ["ip", "link", "set", self.iface, "up"],
            ["iw", "dev", self.iface, "set", "channel", str(self.channel)],
        ]
        for cmd in steps:
            ok, out = sh(cmd)
            if not ok:
                self.__exit__(None, None, None)
                raise RuntimeError(f"failed: {' '.join(cmd)}\n{out.strip()}")
        # verify the switch actually took — Intel cards can silently ignore it
        ok, info = sh(["iw", "dev", self.iface, "info"])
        if "type monitor" not in info:
            self.__exit__(None, None, None)
            raise RuntimeError(
                f"{self.iface} did not enter monitor mode (still: "
                f"{'managed' if 'type managed' in info else 'unknown'}).\n"
                "This card's driver likely does not support usable monitor "
                "mode — very common on Intel iwlwifi. See the note the tool "
                "prints on exit for a USB-adapter fix.")
        return self

    def __exit__(self, *exc):
        for cmd in (["ip", "link", "set", self.iface, "down"],
                    ["iw", "dev", self.iface, "set", "type", "managed"],
                    ["ip", "link", "set", self.iface, "up"]):
            sh(cmd)
        if self._nm_released:
            sh(["nmcli", "device", "set", self.iface, "managed", "yes"])
        # nudge NetworkManager to reconnect if it's around
        sh(["nmcli", "device", "connect", self.iface])


# ---------------------------------------------------------------- capture


ETH_P_ALL = 0x0003


class Station:
    __slots__ = ("mac", "frames", "retries", "bytes", "air_us", "rssi",
                 "last", "bssids", "mgmt", "ctrl", "data")

    def __init__(self, mac):
        self.mac = mac
        self.frames = self.retries = self.bytes = 0
        self.air_us = 0.0
        self.rssi = None
        self.last = 0.0
        self.bssids = collections.Counter()
        self.mgmt = self.ctrl = self.data = 0


class Census:
    def __init__(self):
        self.stations = {}
        self.total_air_us = 0.0
        self.total_frames = 0        # frames that parsed
        self.raw_frames = 0          # frames the socket delivered at all
        self.bad_fcs = 0
        self.start = time.time()

    def add(self, rt, d, framelen, now):
        ta = d.get("ta")
        rate = rate_from_info(rt)
        air = (framelen * 8) / rate if rate > 0 else 0     # microseconds
        self.total_air_us += air
        self.total_frames += 1
        if ta is None:
            return                       # ACK/CTS etc — count air, no owner
        st = self.stations.get(ta)
        if st is None:
            st = self.stations[ta] = Station(ta)
        st.frames += 1
        st.bytes += framelen
        st.air_us += air
        st.last = now
        if d["retry"]:
            st.retries += 1
        if rt.get("signal") is not None:
            st.rssi = rt["signal"]
        if d["ftype"] == FT_MGMT:
            st.mgmt += 1
        elif d["ftype"] == FT_CTRL:
            st.ctrl += 1
        else:
            st.data += 1
        if d.get("bssid"):
            st.bssids[d["bssid"]] += 1


def capture_loop(sock, census, stop):
    while not stop[0]:
        try:
            buf = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        now = time.time()
        census.raw_frames += 1
        rt_len, rt = parse_radiotap(buf)
        if rt_len is None:
            continue
        if rt.get("flags", 0) & RT_FLAG_BADFCS:
            census.bad_fcs += 1
            continue
        body = buf[rt_len:]
        # trailing FCS (4 bytes) is included when the Flags field says so
        framelen = len(body)
        d = parse_dot11(body)
        if d is None:
            continue
        census.add(rt, d, framelen, now)


# ---------------------------------------------------------------- UI


SORTS = [("airtime", lambda s: s.air_us),
         ("frames", lambda s: s.frames),
         ("retry%", lambda s: (s.retries / s.frames) if s.frames else 0),
         ("signal", lambda s: (s.rssi if s.rssi is not None else -999))]


def fmt_bytes(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}T"


def run_ui(stdscr, census, iface, channel, oui, csv_writer, deadline=None):
    curses.curs_set(0)
    curses.use_default_colors()
    stdscr.nodelay(True)
    has_color = curses.has_colors()
    if has_color:
        for i, c in enumerate((curses.COLOR_GREEN, curses.COLOR_CYAN,
                               curses.COLOR_YELLOW, curses.COLOR_RED,
                               curses.COLOR_MAGENTA), 1):
            curses.init_pair(i, c, -1)
    cp = (lambda n: curses.color_pair(n)) if has_color else (lambda n: 0)
    sort_idx = 0
    last_log = 0.0

    while True:
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            break
        if ch in (ord("s"), ord("S")):
            sort_idx = (sort_idx + 1) % len(SORTS)
        if deadline and time.time() >= deadline:
            break

        now = time.time()
        elapsed = max(1e-3, now - census.start)
        util = 100 * census.total_air_us / 1e6 / elapsed

        h, w = stdscr.getmaxyx()
        stdscr.erase()
        sort_name, keyfn = SORTS[sort_idx]
        head = (f" wificensus  {iface}  ch {channel}  |  "
                f"{len(census.stations)} devices  "
                f"{census.total_frames} frames  "
                f"channel busy ~{util:.0f}%  "
                f"elapsed {elapsed:.0f}s  |  sort: {sort_name}  "
                f"(s cycle, q quit) ")
        stdscr.addnstr(0, 0, head.ljust(w - 1), w - 1, curses.A_REVERSE)

        col = (f"{'transmitter':17} {'vendor':18} {'air%':>5} {'frames':>7} "
               f"{'retry%':>6} {'bytes':>7} {'rssi':>5} {'m/c/d':>9} bssid")
        stdscr.addnstr(2, 0, col, w - 1, curses.A_BOLD)

        stations = sorted(census.stations.values(), key=keyfn, reverse=True)
        row = 3
        for st in stations:
            if row >= h - 1:
                break
            air_pct = 100 * st.air_us / 1e6 / elapsed
            retry_pct = 100 * st.retries / st.frames if st.frames else 0
            top_bssid = st.bssids.most_common(1)
            bssid = top_bssid[0][0] if top_bssid else ""
            fresh = (now - st.last) < 3
            attr = 0
            if air_pct > 20:
                attr = cp(4) | curses.A_BOLD
            elif air_pct > 5:
                attr = cp(3)
            elif not fresh:
                attr = curses.A_DIM
            line = (f"{st.mac:17} {vendor_of(st.mac, oui):18} "
                    f"{air_pct:>5.1f} {st.frames:>7} {retry_pct:>6.0f} "
                    f"{fmt_bytes(st.bytes):>7} "
                    f"{(str(st.rssi) if st.rssi is not None else '--'):>5} "
                    f"{st.mgmt:>2}/{st.ctrl:>2}/{st.data:<3} {bssid}")
            stdscr.addnstr(row, 0, line, w - 1, attr)
            row += 1

        if row < h - 1:
            stdscr.addnstr(h - 1, 0,
                           " red = >20% airtime (hog)   yellow = >5%   "
                           "dim = idle >3s   m/c/d = mgmt/ctrl/data frames",
                           w - 1, curses.A_DIM)
        stdscr.refresh()

        if csv_writer and now - last_log >= 5:
            last_log = now
            ts = datetime.now().isoformat(timespec="seconds")
            for st in census.stations.values():
                csv_writer.writerow([
                    ts, st.mac, vendor_of(st.mac, oui),
                    round(100 * st.air_us / 1e6 / elapsed, 2), st.frames,
                    st.retries, st.bytes, st.rssi if st.rssi is not None else "",
                    st.mgmt, st.ctrl, st.data,
                    st.bssids.most_common(1)[0][0] if st.bssids else ""])
        time.sleep(0.5)


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--iface", help="wireless interface (default: autodetect)")
    ap.add_argument("--channel", type=int, required=True,
                    help="channel to listen on (e.g. 11, 149)")
    ap.add_argument("--seconds", type=float,
                    help="auto-stop after N seconds (default: run until q)")
    ap.add_argument("--logdir", default="logs",
                    help="write a per-station CSV snapshot every 5 s here")
    ap.add_argument("--no-restore", action="store_true",
                    help="leave the interface in monitor mode on exit")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("wificensus needs root (monitor mode + raw socket).\n"
                 f"  run:  sudo {sys.argv[0]} --channel {args.channel}")
    iface = args.iface or detect_iface()
    if not iface:
        sys.exit("no wireless interface found (try --iface)")

    print(f"NOTE: this drops {iface}'s Wi-Fi connection while it runs; "
          "it reconnects on exit.")
    print(f"putting {iface} into monitor mode on channel {args.channel}...")

    oui = load_oui()
    census = Census()
    csv_file = csv_writer = None
    if args.logdir:
        os.makedirs(args.logdir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(args.logdir, f"census-ch{args.channel}-{stamp}.csv")
        csv_file = open(path, "w", newline="")
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(["time", "mac", "vendor", "air_pct", "frames",
                             "retries", "bytes", "rssi", "mgmt", "ctrl",
                             "data", "bssid"])

    if not phy_supports_monitor_combo():
        print("WARNING: this card does not list 'monitor' in any valid "
              "interface\ncombination (typical of Intel iwlwifi). Monitor "
              "mode will likely\ncapture nothing. Trying anyway — see the "
              "note below if it fails.\n")

    mon = None
    no_frames = False
    try:
        mon = MonitorMode(iface, args.channel)
        mon.__enter__()
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                             socket.htons(ETH_P_ALL))
        sock.bind((iface, 0))
        sock.settimeout(0.5)

        stop = [False]
        import threading
        cap = threading.Thread(target=capture_loop,
                               args=(sock, census, stop), daemon=True)
        cap.start()

        # preflight: real air on this channel delivers frames within a
        # second or two. If nothing arrives, the card isn't truly sniffing
        # (the usual Intel iwlwifi story) — say so instead of sitting mute.
        print("listening for frames (preflight)...")
        t_pre = time.time()
        while time.time() - t_pre < 6 and census.raw_frames == 0:
            time.sleep(0.3)
        if census.raw_frames == 0:
            stop[0] = True
            raise RuntimeError("NO_FRAMES")

        deadline = time.time() + args.seconds if args.seconds else None
        try:
            curses.wrapper(run_ui, census, iface, args.channel, oui,
                           csv_writer, deadline)
        except KeyboardInterrupt:
            pass
        stop[0] = True
    except RuntimeError as e:
        if str(e) == "NO_FRAMES":
            no_frames = True
        else:
            print(f"\nsetup failed: {e}")
    finally:
        if mon and not args.no_restore:
            mon.__exit__(None, None, None)
        if csv_file:
            csv_file.close()

    if no_frames or (census.raw_frames == 0 and mon):
        print("\n" + "=" * 66)
        print("NO FRAMES CAPTURED. The interface was put into monitor mode "
              "but the\ndriver delivered zero frames — so this card can't "
              "actually sniff.")
        print("This is the norm on Intel iwlwifi cards (yours included).")
        print("\nThe reliable fix is a cheap USB Wi-Fi adapter with a "
              "monitor-capable\nchipset, e.g.:")
        print("  * Alfa AWUS036ACM  (MediaTek MT7612U, 2.4+5 GHz)  ~$30")
        print("  * Alfa AWUS036NHA  (Atheros AR9271, 2.4 GHz only)  ~$15")
        print("Plug it in, find its name with `iw dev`, and run:")
        print(f"  sudo ./wificensus.py --iface <usbwlan> --channel {args.channel}")
        print("\nMeanwhile wifimon.py + wifianalyze.py (this laptop's own "
              "link) work\nfine and already told us a lot.")
        print("=" * 66)
        return

    # final text summary
    elapsed = max(1e-3, time.time() - census.start)
    print(f"\ncaptured {census.total_frames} frames "
          f"({census.raw_frames} raw) from "
          f"{len(census.stations)} devices in {elapsed:.0f}s "
          f"(channel ~{100 * census.total_air_us / 1e6 / elapsed:.0f}% busy)")
    top = sorted(census.stations.values(), key=lambda s: s.air_us,
                 reverse=True)[:12]
    print(f"{'transmitter':17} {'vendor':18} {'air%':>5} {'frames':>7} "
          f"{'retry%':>6}")
    for st in top:
        rp = 100 * st.retries / st.frames if st.frames else 0
        print(f"{st.mac:17} {vendor_of(st.mac, oui):18} "
              f"{100 * st.air_us / 1e6 / elapsed:>5.1f} {st.frames:>7} "
              f"{rp:>6.0f}")
    if csv_file:
        print(f"csv: {path}")
    if args.no_restore:
        print(f"\n{iface} left in monitor mode. restore with:\n"
              f"  sudo ip link set {iface} down && "
              f"sudo iw dev {iface} set type managed && "
              f"sudo ip link set {iface} up")


if __name__ == "__main__":
    main()
