# Wi-Fi Troubleshooting — Review and Next Steps

*Prepared July 2, 2026 · A response to "Wi-Fi Performance Troubleshooting" (copy included alongside this report)*

First off: the investigation so far has been genuinely good. The near-router speed tests, the channel change, the rename-the-network experiment — those were the right instincts, and they ruled out a lot (the fiber, the router's raw speed, the devices themselves, and mostly the smart-home crowd). That's real progress, even when it doesn't feel like it.

Having read through everything, I think the reason it's been so frustrating is simple: **this is two separate problems wearing one trench coat.** Every test so far has been aimed at both at once, so no single fix ever seemed to work.

## The two problems

### Problem 1: Weak signal in the far rooms (living room, deck, bedrooms)

This one is almost certainly the house itself. A 1906 San Francisco house with lath-and-plaster walls is about the worst Wi-Fi environment there is — plaster of that era very often has **wire mesh backing**, which acts like a Faraday cage built into every wall. No channel change, setting tweak, or new router in the same spot will beat physics here.

The good news: the house has Ethernet jacks in the rooms, all running back to a central point. That's the cheat code. A wired access point in the front of the house fixes this properly and permanently. (More below — but let's confirm with measurements before spending money.)

### Problem 2: The Wi-Fi icon disappearing for 5–10 seconds

This is the important one, and here's where I'd gently steer the investigation in a new direction. The previous summary's top theory was interference from the AT&T gateway's Wi-Fi (`Dionysus10`). Interference is real and worth cleaning up — but interference makes Wi-Fi **slow**, not **gone**. A phone on a congested channel will crawl along at 2 Mbps all day without ever dropping the icon. Something else is causing the actual disconnections.

The icon vanishing and coming back on its own has a short list of likely causes, and notice that both observed drops happened in **weak-signal spots** (back deck, living room). That's a big clue. The leading suspects:

1. **The iPhone is hopping between two saved networks.** The phone very likely still remembers `Dionysus10` from before the Synology existed. In the living room, where `Dionysus` is weak, the phone can decide to jump over to `Dionysus10` — and while iOS switches networks, the Wi-Fi icon disappears for several seconds. If the AT&T gateway hands out its own addresses, the switch also breaks whatever the phone was doing. This fits the symptom almost perfectly, and it's a 30-second check.

2. **Smart Connect is shoving the phone around.** The Synology's Smart Connect moves devices between 2.4 GHz and 5 GHz, and the way it does that is blunt: it briefly kicks the device off so it reconnects on the other band. At the edge of coverage — exactly the living room and deck — it can get trigger-happy. That would produce short drops, on both bands, mostly in the far rooms. Which is exactly what's been observed.

3. **iOS's "Private Wi-Fi Address" feature.** Newer iPhones can periodically *rotate* their Wi-Fi hardware address, which forces a brief reconnect. Also a 30-second check.

Here's the encouraging part: these two problems feed each other. The weak signal in the front rooms is what *triggers* the network-hopping and the Smart Connect kicks. Fix the coverage, and the drops most likely go away too.

## What to do next (cheap and easy first)

### Step 1 — On the iPhone (2 minutes, do this first)

- Settings → Wi-Fi → tap **Edit** (top right) to see saved networks. **Forget `Dionysus10`** and any other old networks from the house (old names, test networks, etc.).
- Tap the ⓘ next to `Dionysus` → set **Private Wi-Fi Address** to **Fixed**.
- Optional, just while testing: Settings → Cellular → scroll to the bottom → turn off **Wi-Fi Assist**.

### Step 2 — Turn off the AT&T gateway's Wi-Fi

The previous summary was right about this step — just for a slightly different reason. It cleans up the airwaves *and* removes `Dionysus10` as a place for the phone to escape to. While logged into the gateway, also check that it's in **IP Passthrough** mode (so the Synology is the one true router). Don't unplug the gateway itself — it's still needed for the fiber.

### Step 3 — When a drop happens, catch it in the act

The Synology keeps a log. Right after the icon disappears, note the time, then later check **SRM → Network Center → Log** for entries around that moment. If it says the router *deauthenticated* the phone, that's Smart Connect doing it. If the phone just left, that points back at the phone. This single log line is the most valuable piece of evidence we don't have yet.

### Step 4 — If drops continue: split the bands cleanly

Turn Smart Connect off and give the two bands their own names (e.g. `Dionysus` and `Dionysus-5G`). Last time this was tried, old leftover settings ("New Highland") made a mess and 5 GHz refused to appear — that's a sign the router's wireless config has some cruft. If it misbehaves again, the fix is to reset just the Wi-Fi settings and set them up fresh (not a full factory reset). With the phone pinned to a single band, if the drops stop, we've found our culprit.

### Step 5 — Measure the signal room by room, *then* buy hardware

This is the one measurement the investigation hasn't taken yet, and it's the decider. Speed tests mix everything together; a signal reading separates the problems. On the Mac, hold **Option and click the Wi-Fi icon** in the menu bar — it shows an RSSI number (like -58). Walk to each trouble spot and write it down:

| Location | RSSI (Option-click Wi-Fi icon) |
|---|---|
| Next to router | |
| Kitchen | |
| Living room | |
| Back deck | |
| Upstairs bedroom | |
| Lower bedroom | |

Rough guide: **-50s = great, -60s = fine, -70s = struggling, -80s = barely hanging on.** If the living room and deck read -70 or worse (likely, given the walls), then a **wired access point** using the existing Ethernet is the right fix — one AP near the front of the house, maybe a second upstairs. The Synology stays as the main router. (The WRX560 suggestion from before is a reasonable choice; there are cheaper options too. No need to decide until the numbers are in.)

## A small tool to make this easier

David is putting together a little logging script that can run on the Mac and quietly record the Wi-Fi signal, network name, and connection status once per second. Then the next time the icon vanishes, we'll have a black-box recording of exactly what happened in the seconds before — no more guessing. That's coming next.

## The short version

- The fiber, the router, and your devices are all fine. You proved that already.
- The far rooms are slow because of the walls — old-house problem, and the Ethernet in the walls is the fix.
- The dropouts are most likely the phone hopping to the old AT&T network, or the router shoving the phone between bands — both fixable with settings, and both easiest to trigger in exactly the rooms where the signal is weak.
- Do the two-minute iPhone cleanup first, turn off the AT&T Wi-Fi, and grab the signal numbers when you get a chance. Those numbers will tell us the rest of the story.

Nearly there. 🍷
