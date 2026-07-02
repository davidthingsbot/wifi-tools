# wifi-tools

Tools and documents for debugging a household Wi-Fi problem: intermittent
signal drop-offs (phone loses Wi-Fi for 5–10 s, plus poor speeds in far
rooms) on a Synology RT6600ax in a 1906 lath-and-plaster San Francisco
house.

## wifimon.py

Full-screen temporal Wi-Fi RF monitor for Linux. Runs on a laptop near the
trouble spot; when a device drops off Wi-Fi, glance at the screen to see
whether there was a corresponding RF-level event.

```
./wifimon.py                 # full-screen TUI (best in a maximized terminal)
./wifimon.py --headless 600  # log for 10 minutes, no UI
./wifimon.py --debug-once    # one text sample (sanity check / parsing test)
./wifimon.py --track 10:2c:b1:69:64:ef   # fox-hunt mode (see below)
```

### Fox-hunt mode (`--track BSSID`)

For physically locating a transmitter. Shows one giant live signal
readout for a single BSSID, a WARMER/COLDER indicator, a distance
thermometer, and the best-so-far marker. Walk the house; the number grows
as you approach the device. Scans run at 3 s cadence in this mode, and a
"NOT SEEN for Ns" state distinguishes *device idle* (it stopped beaconing)
from *getting colder* — important for intermittent gadgets. `r` resets
the best-so-far, `q` quits. Logging continues as usual.

Panels:

- **2.4 GHz / 5 GHz spectrum** — every visible AP by channel; bar height =
  strongest signal, bottom row = AP count per channel (yellow when crowded).
- **Timeline** (the main panel) — last N minutes, one column per second:
  - `rssi` — our link signal; red `x` = disconnected
  - `router` — RTT of a 1 Hz ping to the router: the Wi-Fi hop measured
    in isolation (0–100 ms scale). Healthy air is a few ms; swelling RTT
    here is airtime congestion you feel before anything disconnects. A
    red `✕` means the router didn't answer *while still associated* —
    the moment when the phone shows full bars but nothing works.
  - `internet` — RTT of a 1 Hz ping to 1.1.1.1: the whole path (0–500 ms
    scale). A magenta `✕` means the router answered but the internet
    didn't — the problem is past the router, and no channel change will
    fix it. (Red `✕` here just mirrors a router loss: the root cause is
    the Wi-Fi hop.)
  - `traffic` — this machine's own throughput (rx+tx Mb/s) from the
    kernel byte counters. The tell-apart chart: if `router` RTT and
    retries climb whenever `traffic` does, the congestion is
    self-inflicted (bufferbloat); if they're terrible while `traffic` is
    flat, the air itself is hostile. `wifianalyze` bins this
    automatically ("own traffic vs. air quality").
  - `retry%` — tx retransmission rate (5 s window). High = hostile air:
    collisions, overlapping-channel interference, non-Wi-Fi noise.
  - `beac%` — beacons received vs expected. Beacons are the AP's 10 Hz
    heartbeat; missing them is what makes clients give up and disconnect.
  - `busy%` / `noise` replace `beac%` automatically when the driver
    provides survey data (many laptop radios don't).

  Reading the stack top-to-bottom is reading the network stack itself:
  radio (rssi) → link usability (router) → the actual internet →
  our own load (traffic) → the air itself (retry/beacon). On short
  terminals the less-critical charts are dropped first (beac%, then
  router, then traffic); maximize the window to see all six.
- **Events** — disconnects, reconnects, roams, RSSI drops ≥12 dB, retry
  storms, beacon loss, strong APs vanishing.

Everything is also logged to CSV under `./logs/` (1 Hz link samples, every
scan result, every event) for post-mortem analysis.

Requirements: Python 3 (stdlib only), `iw`, and NetworkManager's `nmcli`
for unprivileged scanning. Run with `sudo` for direct nl80211 scans (and,
on some radios, survey noise/busy data).

Notes learned the hard way (Intel iwlwifi):

- `survey dump` returns nothing as a regular user — hence retry%/beacon%
  as the primary air-quality metrics; they work everywhere.
- The firmware rewinds the `beacon rx` counter every ~6 s; wifimon
  detects the rewind and skips that tick rather than misreading it as a
  reassociation.
- Scan results from NetworkManager's cache are often partial; the display
  uses the union of the last two scans and AP-lost events require two
  consecutive misses.

## wifimon-mac.py (for the Mac in the house)

Same screen, same CSV files, adapted to what macOS allows. Getting
started, in Terminal (Applications → Utilities → Terminal), from the
wifi-tools folder:

```
python3 wifimon-mac.py --doctor
```

The doctor checks every data source and prints exact instructions for
anything locked: what to `pip3 install` (optional), where to click in
System Settings if macOS is hiding network names (Privacy & Security →
Location Services → enable for Terminal, then reopen Terminal), and when
`sudo` helps. **The tool runs regardless** — every missing permission
just removes one nicety, and the startup banner says which.

```
python3 wifimon-mac.py                  # the monitor
sudo python3 wifimon-mac.py             # richest link data
python3 wifimon-mac.py --track "Name"   # fox hunt by network name or BSSID
python3 wifimon-mac.py --debug-once     # one text sample to send back
```

Differences from the Linux version:

- **`noise` chart is real** — Mac radios report the noise floor, which
  Intel laptop radios don't. A nearby non-Wi-Fi transmitter shows up
  here directly.
- **No `retry%`/`beac%`** — Apple exposes no station counters. The
  `rate` chart (negotiated tx rate) stands in: rate collapsing while
  rssi holds steady means the radio is drowning in retries, and a
  RATE COLLAPSE event fires.
- **`router`/`internet` ping charts: identical.** So are the CSVs —
  `wifianalyze.py` reads Mac captures unchanged (its suspects table
  automatically uses gateway RTT as the bad-air metric when retry% is
  absent).
- Scans are slower (macOS throttles them) and may show names as
  `<hidden>` until Location Services is granted — `--doctor` explains.

## wificensus.py — who's using the air (monitor mode)

`wifimon` measures *this machine's* link. `wificensus` measures
**everyone's** — it puts the radio into monitor mode and passively sniffs
one channel, then shows a live table of every transmitting device:
airtime %, frame count, retry rate, bytes, signal, and the AP it's
talking to. This is how you tell "the channel is congested because the
AP's own clients are busy" from "something is jamming the air."

```
sudo ./wificensus.py --channel 11               # 2.4 GHz ch 11
sudo ./wificensus.py --channel 149 --seconds 60 # 5 GHz, auto-stop
```

Keys: `q` quit, `s` cycle sort (airtime / frames / retries / signal).
Sorted by airtime by default; devices over 20% airtime turn red (hogs).

**Four things to know before running it:**

1. **It drops this machine's Wi-Fi** while running — a single radio can't
   be associated and sniffing at once. The connection restores on exit
   (so don't run it over SSH-over-Wi-Fi). Run it in bursts.
2. **It sees headers, not contents.** WPA2/WPA3 encrypts payloads. You
   get who-talks-to-whom, sizes, retries, rates, and airtime — never
   what's inside. That's all congestion diagnosis needs.
3. **One channel at a time** — point `--channel` at the AP's channel.
4. **Needs `sudo`** (monitor mode + raw socket). It tells you if not.

Airtime is an estimate (frame bytes ÷ data rate), excellent for ranking
who dominates the channel, not an exact duty cycle. Vendor names come
from a local OUI database if one is installed (`ieee-data` /
`wireshark-common`); otherwise the column is blank and you have the MAC.
A per-station CSV snapshot is written to `logs/` every 5 s.

## wifianalyze.py

Scorecards and forensics for wifimon captures — turns the raw CSVs into a
report:

```
./wifianalyze.py                   # newest capture in ./logs
./wifianalyze.py 20260702-074007   # a specific capture (by stamp)
./wifianalyze.py A B C             # several captures -> comparison table
```

Sections: overview (connectivity, channels, drop count), per-segment
stats, hourly medians, **disconnect forensics** (the last 60 s of RSSI /
retry% / beacon% before every drop), the **suspects table**, and an event
histogram. Run one capture per experiment (channel change, device
unplugged, new location) and compare them side by side.

### Reading the suspects table

For every AP that was sometimes-visible-sometimes-not, three independent
questions, one column each:

- **assoc** — *is bad air associated with this AP being around?* The
  probability that a random minute with the AP visible has worse retry%
  than a random minute without it (computed on per-minute medians).
  50% = unrelated, 60% = mild, 75% = strong, 90%+ = near-lockstep.
- **conf** — *could that be luck?* A Mann-Whitney z-score in sigmas,
  driven by how many minutes of evidence exist on each side. <2σ is
  shrug territory; ≥4σ is solid.
- **heard at** — *is it physically capable of being the cause?* The
  strongest beacon received, as a word (faint/weak/moderate/loud/
  BLASTING) plus dBm.

An AP is a **likely suspect** only when all three hold (assoc ≥60%,
conf ≥2σ, heard at ≥ -72 dBm). One separate carve-out: anything heard at
**-50 dBm or louder is listed regardless of correlation** ("extreme
transmitters") — a bursty device like a camera base station can wreck
the air while beaconing too rarely for presence-correlation to catch it,
and something that loud is in the room with you either way. The table is
for choosing the next unplug-and-measure experiment, not for convicting.

## Documents

- `wifi-report.md` / `wifi-report.pdf` — review of the investigation so
  far and the recommended next steps.
- `Wi-Fi Performance Troubleshooting.pdf` — the original incoming
  investigation summary this repo responds to.
