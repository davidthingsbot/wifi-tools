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
  - `lag` — end-to-end truth: 1 Hz pings to both the gateway and
    1.1.1.1. The chart shows internet RTT; a red `✕` means the *gateway*
    was unreachable while associated (the Wi-Fi hop failed — the moment
    when the phone shows full bars but nothing works), a magenta `✕`
    means the gateway answered but the internet didn't (the problem is
    past the router — no channel change will fix it).
  - `retry%` — tx retransmission rate (5 s window). High = hostile air:
    collisions, overlapping-channel interference, non-Wi-Fi noise.
  - `beac%` — beacons received vs expected. Beacons are the AP's 10 Hz
    heartbeat; missing them is what makes clients give up and disconnect.
  - `busy%` / `noise` replace `beac%` automatically when the driver
    provides survey data (many laptop radios don't).

  Reading the stack top-to-bottom is reading the network stack itself:
  radio (rssi) → air (retry/beacon) → link usability (gateway ping) →
  the actual internet (1.1.1.1). Each layer can lie; the one below it
  can't.
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
