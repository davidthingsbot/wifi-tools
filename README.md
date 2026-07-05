# wifi-tools

A small suite of command-line tools for diagnosing Wi-Fi problems —
intermittent disconnects, "full bars but nothing loads," slow speeds in far
rooms, mystery interference — by **recording what the radio environment
actually does, second by second**, and then reading the evidence back.

Born from a real household case: a phone that lost Wi-Fi for 5–10 s at a
time, plus poor speeds in far rooms, on a Synology RT6600ax mesh in a 1906
lath-and-plaster San Francisco house. See `wifi-report.md` for how these
tools cracked it (the culprit was a security-camera base station shouting
over the whole band). This README is the manual; that report is the worked
example.

> **Reading this as an AI/agent?** Start with [The mental model](#the-mental-model)
> and [Which tool, when](#which-tool-when), then jump to
> [Interpreting the logs](#interpreting-the-logs) and
> [Diagnosis playbook](#diagnosis-playbook) — those two sections are written
> to let you reason from a CSV capture to a root cause without having run
> the tools yourself.

---

## The mental model

Wi-Fi failures live at different layers, and the whole point of these tools
is to **tell the layers apart**. When someone says "the Wi-Fi is broken,"
they could mean any of five very different things:

| Layer | Question | Where you see it |
|---|---|---|
| **Radio** | Is the signal strong enough? | `rssi` (dBm) |
| **The air** | Is the channel usable, or full of noise/contention? | `retry%`, `beacon%`, `noise`, `busy%` |
| **The Wi-Fi hop** | Does the *router* answer? | `router` ping (gateway RTT) |
| **The internet** | Does the path *past* the router work? | `internet` ping (1.1.1.1 RTT) |
| **Self-inflicted?** | Is *our own traffic* causing the pain? | `traffic` (own Mb/s) vs. the above |

The classic trap is conflating the first row with the rest. A strong signal
(good `rssi`, "full bars") tells you the radio can *hear* the AP — it says
**nothing** about whether the air is usable or whether packets get through.
Most mysterious "the Wi-Fi randomly dies" problems are an *air* problem
(interference, contention) happening at *full signal*. These tools exist to
make that distinction visible and logged.

The four tools measure at different scopes:

- **`wifimon.py`** — *this machine's* link, every second, live + logged.
  The flight recorder. Start here.
- **`wifianalyze.py`** — reads wifimon's logs back and produces a report:
  scorecards, disconnect forensics, and a ranked list of suspect APs.
- **`wificensus.py`** — *everyone's* airtime on one channel (monitor mode).
  Answers "who is hogging the channel?" when wifimon says the air is bad but
  not why.
- **`wifimon-mac.py`** — the macOS port of wifimon (same screen, same CSVs).

---

## Which tool, when

```
Symptom / question                          Reach for
──────────────────────────────────────────  ─────────────────────────────
"Wi-Fi keeps dropping / is flaky"           wifimon.py  (leave it running)
"Full bars but pages won't load"            wifimon.py  (watch router/internet)
"Is it my ISP or my Wi-Fi?"                 wifimon.py  (router vs internet ping)
"What happened overnight?"                  wifianalyze.py  (read the capture)
"Did changing channel X help?"              wifianalyze.py A B  (compare captures)
"Which device is hogging the channel?"      wificensus.py  (monitor mode)
"Where is this rogue transmitter?"          wifimon.py --track BSSID  (fox hunt)
"Which mesh node am I actually on?"         wifimon.py  (status line shows #1/#2)
```

**Typical workflow:**

1. Run `wifimon.py` where the trouble is, and leave it (hours, ideally
   through a failure). It logs to `./logs/` continuously.
2. When something breaks, glance at the live screen — or later, run
   `wifianalyze.py` to get the forensic report of what the RF did around
   each drop.
3. If the report fingers a suspect AP, `wifimon.py --track <bssid>` to walk
   toward it, and/or `wificensus.py --channel N` to see who's using the air.
4. Change one thing (channel, unplug a device, move an AP), capture again,
   and `wifianalyze.py old new` to compare side by side. **One capture per
   experiment.**

---

## Requirements

- **Python 3** (standard library only — no `pip install` needed for the
  Linux tools).
- **`iw`** — `sudo apt install iw`. Required by every Linux tool.
- **`nmcli`** (NetworkManager) — optional, lets `wifimon` scan without root.
- Running **as root (`sudo`)** unlocks: direct nl80211 scans, and on some
  drivers the survey `noise`/`busy%` data. `wificensus` *requires* root.
- **A monitor-mode-capable radio** for `wificensus` only. Many laptop Intel
  `iwlwifi` cards **cannot** do this even though `iw` lists the mode — see
  that tool's section. The other three tools work on any normal Wi-Fi card.

Everything is stdlib; there is nothing to build. Make the scripts executable
(`chmod +x *.py`) or run them as `python3 wifimon.py`.

---

## wifimon.py — the flight recorder

Full-screen live monitor of *this machine's* Wi-Fi link, sampled at 1 Hz and
logged to CSV. Run it on a laptop near the trouble spot and leave it; when a
device drops off Wi-Fi, the screen (and the logs) show whether there was a
matching RF-level event.

```bash
./wifimon.py                        # full-screen TUI (maximize the terminal)
./wifimon.py --headless 600         # log for 600 s, no UI (unattended capture)
./wifimon.py --debug-once           # print one text sample and exit (sanity check)
./wifimon.py --track 10:2c:b1:69:64:ef   # fox-hunt one BSSID (see below)
./wifimon.py --iface wlan0          # force an interface (default: autodetect)
./wifimon.py --scan-interval 5      # seconds between AP scans (default 10)
sudo ./wifimon.py                   # direct scans + (some cards) noise/busy data
```

**Keys:** `q` quit · `p` pause/resume the display (logging keeps going).

### The screen, panel by panel

Everything is one column = one second, scrolling right to left.

- **2.4 GHz / 5 GHz spectrum** (top) — every visible AP by channel. Bar
  height = strongest signal on that channel; the bottom row is the AP count
  per channel (turns hot/yellow when a channel is crowded). Bars use the
  band palette (2.4 = orange, 5 = blue) matching the timeline. The AP you're
  connected to is bold; the rest are dimmed. This is your "how crowded is the
  neighborhood" view.

- **Timeline** (the big center panel) — stacked charts, read **top to bottom
  as the network stack itself**:
  - **`rssi`** — your link signal in dBm. `-30` is excellent, `-67` is the
    usual "still fine" floor, `-80`+ is trouble. A red `x` = disconnected.
    Bars are tinted by band so a roam that changes band recolors the signal.
  - **`band`/`chan`** — which band + channel you're on. The label is written
    at each change, then a rule runs until the next change, so band/channel
    hops (mesh roaming) jump out. Once a mesh is known (>1 node on the SSID)
    the node id (`#1`, `#2`) is shown too — so you always know *which* AP.
  - **`router`** — RTT of a 1 Hz ping to the gateway: **the Wi-Fi hop in
    isolation** (0–100 ms scale). Healthy air is a few ms. A red `✕` means
    the router didn't answer *while still associated* — the "full bars but
    nothing works" moment, and the single most diagnostic signal here.
  - **`internet`** — RTT of a 1 Hz ping to `1.1.1.1`: **the whole path**
    (0–500 ms scale). A magenta `✕` means the router answered but the
    internet didn't — the problem is *past* the router (ISP/WAN), and no
    channel change will fix it. A red `✕` here just mirrors a router loss.
  - **`traffic`** — this machine's own throughput (rx+tx Mb/s) from the
    kernel byte counters. **The tell-apart chart:** if `router` RTT and
    `retry%` climb *whenever `traffic` does*, the congestion is
    self-inflicted (bufferbloat / a big download). If they're terrible while
    `traffic` is flat, the air itself is hostile (neighbors, interference).
  - **`retry%`** — tx retransmission rate over a 5 s window. High = hostile
    air: collisions, overlapping-channel interference, non-Wi-Fi noise. This
    is the workhorse air-quality metric because it works on every driver.
  - **`beac%`** — beacons received vs. expected. Beacons are the AP's ~10 Hz
    heartbeat; missing them is exactly what makes a client decide the network
    is gone and disconnect. Low `beac%` precedes drops.
  - **`busy%` / `noise`** — shown *instead of* `beac%` when the driver
    provides survey data (many laptop radios don't). `noise` is the noise
    floor in dBm (higher = louder interference); `busy%` is channel airtime.

  On a short terminal the less-critical charts drop first (`beac%`, then
  `router`, then `traffic`). Maximize the window to see all of them.

- **Events** (bottom) — a running log of anomalies: disconnects, reconnects,
  roams, RSSI drops ≥12 dB, retry storms, beacon loss, link stalls, internet
  loss, strong APs vanishing. Each is also written to the events CSV.

### Mesh: which node am I on?

With several APs broadcasting one SSID (a mesh, e.g. Synology WiFi Point),
`wifimon` groups BSSIDs into physical **nodes** — the radios of one box share
their first five MAC octets — and names them `#1`, `#2`, … The connected node
shows in the status line (`Dionysus #2`) and roam events name both ends
(`ROAM #1 ch 11 -> #2 ch 149`).

The mapping lives in **`ap-nodes.json`** next to the tool. Edit the `"name"`
values to give nodes human names (`"attic"`, `"hall"`) — the new names then
appear everywhere, including `wifianalyze`'s segments table.

> **Why this matters:** `traceroute` can **not** tell mesh nodes apart — mesh
> points are layer-2 bridges, so every packet takes the same IP path. The
> BSSID is the *only* ground truth for which physical box you're on. If drops
> correlate with being on one specific node, that's a finding you can't get
> any other way.

### Fox-hunt mode (`--track BSSID`)

For **physically locating a transmitter** (a rogue device, or the mesh node
you want to reposition). Shows one giant live signal readout for a single
BSSID, a WARMER/COLDER indicator, a distance thermometer, and a best-so-far
marker. Walk the house; the number grows as you approach. Scans run at 3 s
cadence here. A "NOT SEEN for Ns" state distinguishes *device idle* (it
stopped beaconing) from *getting colder* — important for bursty gadgets.
`r` resets the best-so-far, `q` quits. Logging continues.

### Notes learned the hard way (Intel iwlwifi)

- `survey dump` returns nothing as a regular user → that's why `retry%` /
  `beac%` are the primary air-quality metrics; they work everywhere.
- The firmware rewinds the `beacon rx` counter every ~6 s; wifimon detects
  the rewind and skips that tick rather than misreading it as a
  reassociation.
- NetworkManager's cached scan results are often partial; the display uses
  the union of the last two scans, and AP-lost events require two consecutive
  misses (so weak APs flickering in and out don't spam the log).

---

## wifianalyze.py — read the capture back

Turns the raw CSVs into a text report: scorecards, per-segment stats,
disconnect forensics, a suspects table, and (with several captures) a
side-by-side comparison. This is where a long unattended `wifimon` capture
becomes an answer.

```bash
./wifianalyze.py                     # newest capture in ./logs
./wifianalyze.py 20260702-074007     # a specific capture (by timestamp stamp)
./wifianalyze.py logs/link-....csv   # by path
./wifianalyze.py A B C               # several captures → comparison table
./wifianalyze.py --suspects-top 15   # show more suspect rows
./wifianalyze.py --suspects-all      # include the non-credible correlations
```

Sections, in order:

- **overview** — duration, % connected, channels used, drop count, and
  median/p90 for rssi / retry / beacon / noise / gateway RTT / internet RTT.
- **segments** — each contiguous `(BSSID, channel)` stretch with its median
  RSSI / retry% / beacon%. Shows how the link behaved on each node/channel.
- **hourly** — median retry% and beacon% by hour of day, with a bar. Reveals
  time-of-day patterns (e.g. "every evening at 8pm").
- **disconnects** — for every drop, the **last 60 s of RF before it**
  (rssi / retry% / beacon% / gateway ms / internet ms at −60s, −50s, …), plus
  how long the outage lasted. This is the money section: it shows *what
  failed first*.
- **own traffic vs. air quality** — bins every second by your own throughput
  (idle / light / heavy) and shows retry% + latency in each bin. If they rise
  with your own traffic, the problem is self-inflicted; if they're bad even
  when idle, blame the air.
- **suspects** — every intermittent AP ranked by how much worse the air is
  while it's visible (see below).
- **event histogram** — counts of each event type.

### Reading the suspects table

For every AP that was *sometimes* visible, three **independent** questions,
one column each — and an AP is only a likely suspect when **all three** hold:

- **assoc** — *is bad air associated with this AP being around?* The
  probability that a random minute with the AP visible has worse `retry%`
  than a random minute without it (on per-minute medians). 50% = unrelated,
  60% = mild, 75% = strong, 90%+ = near-lockstep.
- **conf** — *could that be luck?* A Mann-Whitney z-score in sigmas, driven
  by how many minutes of evidence exist on each side. <2σ = shrug; ≥4σ =
  solid.
- **heard at** — *is it physically capable of being the cause?* The strongest
  beacon received, as a word (faint / weak / moderate / loud / BLASTING) plus
  dBm. A faint AP is rarely the physical culprit even at high `assoc` (that
  usually means *shared timing* — it and the real interferer are both busy in
  the evening).

A high number in **one** column means nothing; a real suspect needs all
three (assoc ≥60%, conf ≥2σ, heard at ≥ −72 dBm).

**One carve-out:** anything **heard at −50 dBm or louder is listed
regardless** of correlation ("extreme transmitters") — a bursty device (a
camera base station, a hub) can wreck the air while beaconing too rarely for
presence-correlation to catch it, and something that loud is *in the room
with you* either way. This is exactly how the Eufy base station in the case
study got flagged.

The table is for choosing the next **unplug-and-measure experiment**, not for
convicting. Correlation, not proof.

> Note: on captures with no `retry%` (macOS has no station counters), the
> suspects table automatically switches to gateway RTT as the bad-air metric
> — a struggling Wi-Fi hop inflates gateway RTT the same way.

---

## wificensus.py — who's using the air (monitor mode)

`wifimon` measures *this machine's* link. `wificensus` measures
**everyone's** — it puts the radio into monitor mode, passively sniffs one
channel, and shows a live table of every transmitting device: airtime %,
frame count, retry rate, bytes, signal, and the AP it's talking to. This is
how you tell *"the channel is congested because the AP's own clients are
busy"* from *"something is jamming the air."*

```bash
sudo ./wificensus.py --channel 11               # 2.4 GHz, channel 11
sudo ./wificensus.py --channel 149 --seconds 60 # 5 GHz, auto-stop after 60 s
sudo ./wificensus.py --channel 6 --iface wlp0s20f3
```

**Keys:** `q` quit · `s` cycle sort (airtime / frames / retries / signal).
Sorted by airtime; devices over 20% airtime turn red (hogs), over 5% yellow.
The header shows total channel busy %. A per-station CSV snapshot is written
to `logs/` every 5 s.

**Five things to know before you run it:**

0. **Your card may not be able to do this at all.** Monitor mode must be
   supported by the driver, and **Intel `iwlwifi` cards (common in laptops)
   usually can't deliver monitor frames** even though `iw` lists the mode.
   The tool checks the card's interface combinations up front, and if it
   captures nothing it says so plainly and points you at a cheap USB adapter
   (Alfa AWUS036ACM ~$30, or AWUS036NHA ~$15) that works reliably. This is a
   hardware limit, not a bug.
1. **It drops this machine's Wi-Fi while running** — a single radio can't be
   associated *and* sniffing. The connection restores on exit (so don't run
   it over SSH-over-Wi-Fi). Run it in bursts.
2. **It sees headers, not contents.** WPA2/WPA3 encrypts payloads. You get
   who-talks-to-whom, sizes, retries, rates, and airtime — never what's
   inside. That's all congestion diagnosis needs.
3. **One channel at a time** — point `--channel` at the AP's channel.
4. **Needs `sudo`** (monitor mode + raw socket).

Airtime is an *estimate* (frame bytes ÷ data rate) — excellent for ranking
who dominates the channel, not an exact duty cycle. Vendor names come from a
local OUI database if one is installed (`ieee-data` / `wireshark-common`);
otherwise that column is blank and you have the MAC.

The `m/c/d` column is management / control / data frame counts. A device that
is nearly all **management** frames (with few data frames) but high airtime is
a beaconing/probing nuisance rather than a real user — a useful tell.

---

## wifimon-mac.py — the macOS sibling

Same screen, same CSV files (`wifianalyze.py` reads Mac captures unchanged),
adapted to what macOS allows. Start with the doctor:

```bash
python3 wifimon-mac.py --doctor        # checks every data source, tells you
                                       # exactly what to install/click
python3 wifimon-mac.py                 # the monitor
sudo python3 wifimon-mac.py            # richest link data
python3 wifimon-mac.py --track "Name"  # fox hunt by network name or BSSID
python3 wifimon-mac.py --debug-once    # one text sample
```

**The tool runs regardless** of permissions — each missing grant just removes
one nicety, and the startup banner says which. Feature parity with Linux:
router/internet ping charts, own-`traffic` chart, and mesh-node
identification all work. macOS-specific differences:

- **`noise` chart is real** — Mac radios report the noise floor (Intel laptop
  radios don't). A nearby non-Wi-Fi transmitter shows up here directly.
- **No `retry%` / `beac%`** — Apple exposes no station counters. The `rate`
  chart (negotiated tx rate) stands in: rate collapsing while `rssi` holds
  steady means the radio is drowning in retries, and a RATE COLLAPSE event
  fires.
- **Mesh node names need a real BSSID**, so they require Location Services
  granted to Terminal (Privacy & Security → Location Services → enable for
  Terminal, then reopen it) — otherwise macOS redacts BSSIDs and the node
  shows as `?`. `--doctor` walks you through it.
- Scans are slower (macOS throttles them) and names may show as `<hidden>`
  until Location Services is granted.

---

## Interpreting the logs

`wifimon` writes three CSVs per run to `./logs/`, timestamped with the start
time (`link-YYYYMMDD-HHMMSS.csv`, etc.). `wificensus` writes a fourth.
Everything is plain CSV — load it in `wifianalyze`, a spreadsheet, pandas, or
read it straight.

### `link-*.csv` — the 1 Hz link sample (the main log)

One row per second. This is the flight-recorder tape. Columns:

| Column | Meaning | How to read it |
|---|---|---|
| `time` | ISO-8601 second | the timeline |
| `connected` | 1 / 0 | 0 = fully dropped off Wi-Fi |
| `ssid`, `bssid` | network + specific radio | `bssid` identifies the exact node/band |
| `freq` | MHz | 2xxx = 2.4 GHz, 5xxx = 5 GHz |
| `rssi_dbm` | signal | −30 great · −67 ok floor · −80+ weak |
| `txrate_mbps` | negotiated tx rate | collapses under retries even at steady rssi |
| `retry_pct` | tx retransmissions (5 s window) | **key air metric.** <10% good · 30%+ bad · 70%+ storm |
| `beacon_pct` | beacons received vs expected | **key air metric.** 90%+ good · <50% = losing the AP's heartbeat |
| `tx_pkts`, `tx_failed` | per-second counters | context for retry% (was there traffic to retry?) |
| `beacon_loss`, `rx_drop` | driver-reported | non-zero beacon_loss = driver noticed missed beacons |
| `noise_dbm` | noise floor (if available) | −95 quiet · −80+ noisy. Blank on many Linux cards |
| `busy_pct` | channel airtime (if available) | 85%+ = saturated channel. Blank on many Linux cards |
| `gw_rtt_ms` | **router** ping RTT | few ms healthy · tens–hundreds = congested air |
| `inet_rtt_ms` | **internet** (1.1.1.1) RTT | isolates the path past the router |
| `gw_loss` | 1 = router unreachable *while associated* | **the "bars lie" flag** — the most diagnostic column |
| `inet_loss` | 1 = internet unreachable | if 1 while `gw_loss`=0 → problem is past the router |
| `rx_mbps`, `tx_mbps` | own throughput | correlate against the pings to spot self-inflicted lag |

**Blank cells are normal and meaningful.** A blank `retry_pct`/`beacon_pct`
usually means "no traffic that second" or "just (re)associated, re-baselining
the counters," not zero. Blank `noise_dbm`/`busy_pct` means the driver
doesn't expose survey data (very common). Blank pings during
`connected`=0 are expected (offline losses aren't news).

### `scan-*.csv` — every AP seen in each scan

`time, bssid, ssid, freq, chan, signal_dbm`. One row per AP per scan (every
~10 s). Feeds the spectrum panels and the suspects table. Use it to see which
APs appear/vanish and how loud they get. An AP present in only ~half the
scans is *intermittent* — the profile of a bursty interferer.

### `events-*.csv` — the anomaly log

`time, event, detail`. The human-readable highlights. Event types and what
each one means:

| Event | Means | Typical cause |
|---|---|---|
| `DISCONNECT` / `RECONNECT` | left / rejoined the network | the symptom you're chasing |
| `ROAM` | switched BSSID (node or band) | mesh handoff — names both ends |
| `RSSI DROP` | signal fell ≥12 dB fast | moved away, or AP power changed |
| `RETRY STORM` | sustained ≥70% retries (with real traffic) | **hostile air** — interference/contention |
| `BEACON LOSS` | <50% beacons, or driver flagged loss | losing the AP's heartbeat → drop is imminent |
| `LINK STALL` | associated but gateway unreachable ≥3 s | **"bars lie"** — Wi-Fi hop failed at full signal |
| `INET LOSS` | gateway fine, internet gone ≥5 s | problem is **past the router** (ISP/WAN) |
| `LAG` | internet RTT >400 ms sustained | bufferbloat or upstream congestion |
| `NOISE SPIKE` | noise floor above −80 dBm | non-Wi-Fi interferer nearby (needs survey data) |
| `AIRTIME` | channel >85% busy | saturated channel (needs survey data) |
| `AP LOST` | a strong AP (≥ −65 dBm) vanished from scans | an interferer went quiet, or a node rebooted |

### `census-ch*-*.csv` — per-station airtime (from `wificensus`)

`time, mac, vendor, air_pct, frames, retries, bytes, rssi, mgmt, ctrl, data,
bssid`. A snapshot every 5 s of every transmitter on the sniffed channel.
Sort by `air_pct` to find the channel hog; `mgmt`-heavy stations with little
`data` are beaconing nuisances rather than real users.

---

## Diagnosis playbook

Symptom → what to look for in the logs → likely cause. This is the reasoning
`wifianalyze` automates, spelled out so you (or an AI reading a capture) can
do it by hand.

### "Wi-Fi randomly drops, then comes back"

Look at the **last 60 s before each `DISCONNECT`** (wifianalyze's disconnect
forensics, or filter `link-*.csv` to the window). Then branch on what failed
first:

- **`rssi` was fine (better than ~−70) but `retry_pct` climbed to 50–100%
  and `beacon_pct` fell** → **interference, not coverage.** The signal was
  loud, the air was unusable. Missing beacons is what makes the client give
  up. → find the interferer (suspects table, then `--track` / `wificensus`).
- **`rssi` sagged toward −80+ before the drop** → **coverage.** You're at the
  edge of range. → move closer, add an AP, or check whether you should have
  roamed to a nearer node (watch the `band/chan` node id).
- **`gw_loss`=1 while still `connected`=1 (a `LINK STALL`)** → the "bars lie"
  moment: associated, full signal, router not answering. Almost always the
  air (retries) starving the link. Cross-check `retry_pct` in the same
  window.
- **Drops cluster on one `bssid`/node, or right after a `ROAM`** → a specific
  node is bad, or the mesh is roaming you to a worse AP. Segments table shows
  per-node behavior.

### "Full bars but nothing loads"

The signature is **`connected`=1, good `rssi`, but `gw_loss`=1 or `gw_rtt_ms`
in the hundreds** (a red `✕` on the `router` chart / a `LINK STALL` event).
Full bars only means the radio hears the AP; it says nothing about the air.
Check `retry_pct` — if it's high, the air is congested. This is *not* an ISP
problem.

### "Is it my Wi-Fi or my ISP?"

The `router` vs `internet` split answers this directly:

- **`gw_loss`=1 (router unreachable)** → it's your **Wi-Fi / LAN**. Red `✕`.
- **`gw_loss`=0 but `inet_loss`=1 (router fine, internet gone)** → it's
  **past the router** — your ISP/WAN, or DNS. Magenta `✕`. No channel change
  or AP move will help.
- **Both fine but `inet_rtt_ms` is high while `gw_rtt_ms` is low** → the Wi-Fi
  hop is healthy; the latency is upstream (a `LAG` event).

### "Slow speeds, especially in far rooms"

Check `own traffic vs. air quality` in wifianalyze:

- **retry%/latency rise with your own `traffic`** → self-inflicted:
  bufferbloat, a saturated backhaul, or an old-standard device dragging the
  whole channel down. Not an air problem.
- **retry%/latency bad even when idle** → ambient hostility: neighbors or an
  interferer own the air. Run `wificensus` to see who.
- **`rssi` weak in the far room but the air is clean** → pure coverage
  (thick walls). The fix is a **wired AP** closer to the room, not a channel
  change. (This is the "1906 plaster" half of the case study — it survives
  removing the interferer.)

### "Which device is jamming the air?"

1. `wifianalyze` **suspects table** → intermittent APs correlated with bad
   air, or any "extreme transmitter" heard at ≥ −50 dBm.
2. `wifimon.py --track <bssid>` → walk toward it; the number grows. Confirms
   it's a physical, in-house transmitter.
3. `sudo wificensus.py --channel N` → see per-device airtime. A single
   station over 20% (red) or a management-heavy beaconer is your hog.
4. **Unplug the suspect, capture again, and compare** (`wifianalyze old new`).
   The comparison table is the proof — retry% and drops should fall.

### What "fixed" looks like

When you've removed the culprit, a fresh capture should show: `retry_pct`
back under ~20% (from storm levels), `beacon_pct` up in the 90s, **zero
`DISCONNECT` events**, and — the real test — the phone's Wi-Fi icon stops
vanishing. Confirm with a full day's capture, not a few minutes.

---

## Documents

- **`wifi-report.md`** / `wifi-report.pdf` — the worked example: the
  investigation these tools were built for, its findings, and next steps.
  Read it alongside the [Diagnosis playbook](#diagnosis-playbook) to see the
  reasoning applied end to end.
- **`Wi-Fi Performance Troubleshooting.pdf`** — the original incoming
  investigation summary this repo responds to.

## Files at a glance

```
wifimon.py       live per-link monitor + CSV logger (start here)
wifianalyze.py   reads the CSVs back → scorecards, forensics, suspects
wificensus.py    monitor-mode airtime census (who's using the channel)
wifimon-mac.py   macOS port of wifimon (same CSVs)
ap-nodes.json    BSSID→node-name map (edit "name" to rename mesh nodes)
logs/            all CSV output (gitignored)
```
