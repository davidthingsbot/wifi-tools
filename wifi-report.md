# Wi-Fi Troubleshooting — Findings and Fix

*Prepared July 2, 2026, updated the same evening · follows up on "Wi-Fi Performance Troubleshooting" (copy alongside this report)*

## The headline: we found it

The thing that has been knocking devices off the Wi-Fi is a **Eufy security
base station, running in "Experimental High Power" mode**. It operates its
own hidden Wi-Fi network (that's how it talks to its cameras), and in that
mode it transmits *loudly* — from the kitchen laptop's position it measured
**-22 dBm, roughly a thousand times stronger than any neighbor's router**. It
also transmits intermittently: quiet when the cameras are idle, blasting when
they wake. A strong, bursty, in-house transmitter is precisely the profile of
"the Wi-Fi randomly dies and then comes back."

It's now unplugged and the network is being re-measured. Early conclusion to
hold loosely until the baseline confirms it — but the evidence is strong.

## How we caught it

We wrote a small monitoring tool ([wifimon](https://github.com/davidthingsbot/wifi-tools))
that runs on a laptop and records, every second, what the radio environment
looks like. An 11-hour capture showed:

- **The laptop itself dropped off Wi-Fi 8 times in one day** — the same
  symptom as the phone, now with a flight recorder attached.
- **Every drop happened with a strong signal.** In the minute before each
  drop, signal strength was fine, but 60–100% of transmitted frames were
  having to be re-sent, and the laptop was missing 20–50% of the router's
  "heartbeat" beacons. Missing heartbeats is exactly what makes a phone
  decide the network is gone. In short: the signal was loud, and the air
  was unusable — interference, not coverage.
- **A mystery hidden network kept appearing and vanishing** — present in
  only half the scans, absurdly strong, hardware ID belonging to a
  smart-home manufacturer. A "fox hunt" mode (a big live signal readout —
  walk around, the number grows as you get closer) led straight to the Eufy
  base station.

## What this explains — and what it doesn't

**Explains:** the random disconnects on both bands, the "signal bars look
fine but nothing works" moments, the failure of every channel change to fix
things (a transmitter that strong bleeds across channels), and why the
problem seemed to come and go without pattern (it tracked the cameras'
activity, not anything anyone did to the router).

**Doesn't explain:** the *slow speeds in the far rooms*. That is still the
1906 walls — old plaster over wire mesh eats Wi-Fi — and it will still be
true with the Eufy gone. The far rooms will drop less often now, but they
won't get fast. The fix for that remains a **wired access point** in the
front of the house, using the Ethernet already in the walls. Worth doing
once the baseline is confirmed.

## Confirming the fix

With the Eufy unplugged, leave the monitor running for a day. What "fixed"
looks like:

- retransmission rate falling from ~67% to well under half that
- beacon delivery climbing from ~79% into the 90s
- zero laptop disconnects
- and the real test: the phone's Wi-Fi icon stops vanishing

## When the base station comes back

It's presumably guarding something, so it can't stay unplugged forever.
Before it returns:

1. **Turn off Experimental High Power mode.** Normal mode exists for a
   reason; the experimental setting is meant for cameras at the far end of
   a large property, not a base station in the living space.
2. **Check whether the Eufy app lets you pin its channel** — if so, put it
   on whichever 2.4 GHz channel the Synology is *not* using.
3. **Move it away from where people use their devices** — distance from the
   base station matters as much as its power setting. If it has an Ethernet
   port, wire it; that removes at least some of its radio traffic.
4. Re-run the monitor for a day after it returns, and compare.

## Remaining to-do list (from the earlier report — still valid)

- **iPhone hygiene** (if not already done): forget `Dionysus10` and any old
  saved networks; set Private Wi-Fi Address to "Fixed" for `Dionysus`.
- **AT&T gateway Wi-Fi stays off** (it already appears to be off — good),
  and confirm the gateway is in IP-Passthrough mode.
- **Move the Synology's 5 GHz to channel 149** (80 MHz). The current
  channel-40 block is shared with a strong neighboring mesh system;
  149 is nearly empty here, allows higher transmit power, and avoids the
  radar-shared channels that can cause their own disconnects.
- **Wired access point for the front rooms** — the coverage fix, unchanged.

## The moral of the story

Everyone's original instincts — channels, router settings, too many smart
devices — were reasonable, and testing them is what narrowed the field. The
culprit turned out to be a smart-home device, just not by crowding the
network: it was shouting over everyone with a setting labeled
"experimental," on a network no one could see. The tools that found it are
in the repo and ready for the next mystery. 🍷
