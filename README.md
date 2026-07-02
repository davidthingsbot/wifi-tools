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
```

Panels:

- **2.4 GHz / 5 GHz spectrum** — every visible AP by channel; bar height =
  strongest signal, bottom row = AP count per channel (yellow when crowded).
- **Timeline** (the main panel) — last N minutes, one column per second:
  - `rssi` — our link signal; red `x` = disconnected
  - `retry%` — tx retransmission rate (5 s window). High = hostile air:
    collisions, overlapping-channel interference, non-Wi-Fi noise.
  - `beac%` — beacons received vs expected. Beacons are the AP's 10 Hz
    heartbeat; missing them is what makes clients give up and disconnect.
  - `busy%` / `noise` replace `beac%` automatically when the driver
    provides survey data (many laptop radios don't).
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

## Documents

- `wifi-report.md` / `wifi-report.pdf` — review of the investigation so
  far and the recommended next steps.
- `Wi-Fi Performance Troubleshooting.pdf` — the original incoming
  investigation summary this repo responds to.
