# Trancistor

**Turn your smart lights into a live light show that reacts to whatever music is playing — in real time, with no cloud, no pre-analysis, and no fixed light patterns.**

Trancistor is a Home Assistant add-on. It listens to a USB audio input (a line-in from your stereo, a mic in the room, a capture dongle off your TV — anything), figures out *what just happened in the music* several times a second, and fires Home Assistant events. You then wire those events to your lights with ordinary HA automations, so **you** decide exactly how each light behaves.

> This add-on is shared as-is and is not actively maintained — see the repository README for details. It was built and tested on a Raspberry Pi (aarch64) with a USB line-in on Home Assistant OS.

---

## Table of contents

1. [What it does, in plain terms](#what-it-does-in-plain-terms)
2. [How it works (the reasoning)](#how-it-works-the-reasoning)
3. [Installing it](#installing-it)
4. [Connecting your lights (automations)](#connecting-your-lights-automations)
5. [The tuning screen — how to dial it in](#the-tuning-screen--how-to-dial-it-in)
6. [Protecting your Home Assistant storage](#protecting-your-home-assistant-storage)
7. [The events it sends (reference)](#the-events-it-sends-reference)
8. [Advanced tuning (editing the code)](#advanced-tuning-editing-the-code)
9. [Troubleshooting](#troubleshooting)

---

## What it does, in plain terms

Music is a blend of different sounds happening at once: the **kick drum and bass** down low, the **vocals and melody** in the middle, the **cymbals and "air"** up high. Trancistor splits the incoming sound into those three **bands** and watches each one separately. When something *happens* in a band — a kick lands, a cymbal crashes — it sends a signal. When a band just gets generally louder or softer — a vocal swelling — it sends a smoothly changing brightness.

That means you can point:

- a **bass light** at the low band so it punches on every kick,
- a **main light** at the middle band so it breathes with the vocals,
- an **accent light** at the high band so it sparkles with the hi-hats,

…and all three move independently, live, to any song — without you ever programming a pattern.

The key idea: **Trancistor never tells your lights what to do.** It only reports what the music is doing. How bright, what color, how fast a fade — all of that lives in your own Home Assistant automations, where you have total control.

---

## How it works (the reasoning)

You don't need this section to use Trancistor, but it explains *why* it behaves the way it does — which makes tuning make sense.

### It listens in tiny slices

Every ~43 milliseconds (about 23 times a second) the add-on grabs a slice of incoming audio and runs it through a **frequency analysis** (an FFT — the same math that draws the bars on a graphic equalizer). That tells it how much energy is sitting at each pitch, from deep sub-bass up to the highest sparkle.

### It thinks in "louder than a moment ago," not "loud"

Here's the trick that makes it work on *any* song at *any* volume. Trancistor doesn't ask "is this band loud?" — loudness depends on how you set your stereo. Instead it asks **"is this band suddenly louder than it was a fraction of a second ago?"**

A real drum hit is a sharp jump against the last quarter-second. A slow build-up or a steady bass note *isn't* — it changes too gradually to look like a spike. So this "ratio against the recent past" naturally picks out **hits** and ignores **drones**, and it does so whether the music is quiet or blasting. There's no volume knob to set — it adapts itself.

(For the curious: the "recent past" it compares against is a low percentile of the last five slices, which keeps it anchored to the quiet baseline even during a busy, dense drop — so it doesn't "get used to" a beat and stop responding.)

### Every band produces two different signals

For each band you get **both** of these, and you choose which one (or both) to use per light:

- **An onset event** — a discrete "a hit just happened!" moment. Great for a light that **flashes** on the beat.
- **A flow level** — a continuously updated brightness (0–100%) that rises and falls smoothly with the band's intensity. Great for a light that **breathes** with the music instead of blinking.

### Each band is tuned to its job

Not every band should react the same way, so they don't:

- **Low (kick/bass)** fires *instantly* on a spike. Modern kick drums (especially "808s") are a sharp thump welded to a long sustained tail, so waiting to "confirm" the hit would actually throw real kicks away. Low trusts the spike.
- **Mid and High** *confirm* each hit before reporting it: after the spike, the sound has to quickly fall back down. A drum hit does that; a held vocal note or a cymbal wash doesn't. This is what stops your mid light from flashing on every sung word.

### Guardrails so it doesn't flail

Two extra safety mechanisms, both learned from real-world testing:

- **Quiet-section gate.** During a hushed intro or breakdown, the "louder than a moment ago" math can get twitchy — tiny noises look like big jumps. So each band remembers how loud it *recently* got (over ~15 seconds) and ignores events that are only a tiny fraction of that. When the whole track goes quiet, the lights calm down instead of strobing.
- **Kick hardness.** Not every kick hits equally hard. Trancistor measures how strong each kick is *relative to how hard the kicks have been hitting lately* and attaches that number to the event, so your bass light can punch bright on the big drops and stay gentle on the soft kicks — instead of every kick being full-blast.

### The expectation window — catching beats you can hear but the lights miss

Underneath the Low band, an internal tempo tracker watches the timing between real kicks, and once several in a row agree on a steady beat, it "locks" and keeps a running prediction of when the next beat should land. This has no direct output of its own — it exists purely to feed the following trick.

During quieter passages, a kick can still be clearly audible yet too small to clear the normal bar — the quiet-section gate is deliberately conservative. But if the tracker knows a beat is due *right now*, a small peak arriving exactly on cue is almost certainly that beat. So inside a narrow window (±60 ms) around each predicted beat, the bar drops: a much smaller on-cue peak fires a real `trancistor_low` (tagged `(expected)` in the log, `"expected": true` in the event). An equal-size peak *between* beats never sees the lowered bar — being on the grid is what earns the benefit of the doubt.

In offline testing this recovered **all** of the quiet on-grid kicks the gate had been blocking, without a single fire on equal-size off-grid distractors. Since these rescued beats are genuinely quiet, their `hardness` value is naturally small — so if your automation scales brightness by hardness, the recovered beats show up as soft pulses, exactly as they sound.

---

## Installing it

### What you need

- **Home Assistant OS** or **Supervised** (the add-on system must be available — this won't run on HA Container/Core).
- A device HA runs on that reports architecture **aarch64** (e.g. a Raspberry Pi 4/5).
- **A USB audio input** carrying the music: a USB sound card with a line-in from your amplifier, a USB microphone in the room, an HDMI-audio capture dongle, etc. Trancistor reacts to whatever this input hears.

### Step 1 — Add the add-on

If you added this via the repository URL (**Settings → Add-ons → Add-on Store → ⋮ → Repositories**), it will already show up under **Trancistor** in the store — skip to installing it.

To install manually instead: copy this whole folder into your Home Assistant `addons/` share (the folder that holds `config.json`, `Dockerfile`, `beat_detector.py`, and `run.sh`). You can do this over Samba, the SSH add-on, or the File Editor. Then in Home Assistant go to **Settings → Add-ons → Add-on Store**, open the **⋮ menu (top right) → Check for updates**, and your **Local add-ons** section will show **Trancistor**.

Click it and press **Install**. (The first build takes a few minutes while it assembles the container.)

### Step 2 — Point it at your audio input

1. On the add-on page, open the **Audio** tab.
2. Set the **Input** dropdown to your USB capture device.
3. Leave **Output** as-is (Trancistor only listens; it doesn't play anything).

### Step 3 — Start it

1. On the **Info** tab, turn on **Start on boot** and **Watchdog** (recommended), then press **Start**.
2. Open the **Log** tab and confirm it's alive. A healthy log is quiet — it only prints when a beat fires or when something's wrong. Play some music and you should see lines like `LOW (confirmed): ratio=4.20 hard=0.88 …`.

### Step 4 — Open the tuning screen

Click **Open Web UI** (or find **Trancistor** in your HA sidebar). This is where you'll dial everything in — covered in [The tuning screen](#the-tuning-screen--how-to-dial-it-in) below.

---

## Connecting your lights (automations)

Trancistor just fires events; your automations turn those into light behavior. Here are three ready-to-use patterns. Create these under **Settings → Automations → Create Automation → Edit in YAML**.

### A bass light that punches on the kick

This flashes a light on every kick, and — thanks to the **hardness** value — brighter on hard kicks, dimmer on soft ones. It defines the light **once** at the top, so you only edit it in one place.

```yaml
alias: Trancistor - Low (kick pulse)
variables:
  beat_lights:
    - light.window          # <-- your bass light(s); add more with another "- light.xxx"
triggers:
  - trigger: event
    event_type: trancistor_low
conditions:
  # Ignore stale events (only react to a beat from the last 300 ms).
  - condition: template
    value_template: >
      {{ trigger.event.data.ts is not defined or
         (now().timestamp() * 1000 - trigger.event.data.ts | float(0)) < 300 }}
actions:
  # Attack: snap up to a brightness scaled by how HARD the kick hit.
  - action: light.turn_on
    target:
      entity_id: "{{ beat_lights }}"
    data:
      brightness: "{{ (15 + (trigger.event.data.hardness | float(1)) ** 2 * 240) | int }}"
      transition: 0
  - delay:
      milliseconds: 130       # how long it holds before releasing (longer = less blinky)
  # Release: drop back down so the next kick is a fresh, distinct pulse.
  - action: light.turn_off
    target:
      entity_id: "{{ beat_lights }}"
    data:
      transition: 0.3         # fade tail (ignored by lights that can't fade)
mode: restart
```

### A main light that breathes with the vocals/melody

This rides the **flow** level of the mid band — no flashing, just smooth rising and falling brightness that follows the music's intensity.

```yaml
alias: Trancistor - Mid melody flow
variables:
  flow_lights:
    - light.living_room       # <-- your melody light(s)
triggers:
  - trigger: event
    event_type: trancistor_mid_level
actions:
  - action: light.turn_on
    target:
      entity_id: "{{ flow_lights }}"
    data:
      brightness: "{{ trigger.event.data.brightness }}"
      transition: 0.2
mode: restart
```

Swap `trancistor_low` / `trancistor_mid_level` for any of the [events](#the-events-it-sends-reference) to build your own combinations — a high-band sparkle, a low-band *flow* instead of a flash, whatever you like.

> **Tip for many lights at once:** if an automation targets a lot of bulbs, group them at the source — a **Zigbee2MQTT group** or a **Hue zone/room** — and target that single group entity. One command to a group is far easier on your network than a dozen separate commands, and everything changes in perfect sync.

---

## The tuning screen — how to dial it in

The Web UI has two live displays stacked on top, and a control block per band below.

### The two displays

- **Spectrum ("what the mic hears").** A live frequency graph on a log scale, like a graphic EQ. The three shaded regions are your bands. Content *outside* every shaded region can't trigger anything. **Use this to place your band edges** — you can literally watch where the kick's energy sits and drag the low band to cover it.
- **Detector ("what the detector reacts to").** One meter per band showing the "louder than a moment ago" ratio, hanging near its recent peak. Each meter **auto-scales** to what that band actually produces (see the *"bar full = N"* label), so a quiet band still uses the whole bar. The meter outline **flashes green every time that band fires** — so you can see exactly what's triggering.

### The knobs, per band

- **Enabled** — turn the whole band on or off. This silences both the band's hit events *and* its flow stream (the flow sends one final brightness-0 so the light fades out instead of freezing mid-brightness).
- **Sensitivity** — the trigger threshold, shown as a white line on the meter and as a number. Drag it so the white line sits **just above** where real hits peak: lower = fires more easily, higher = more selective. All the way right = the band is muted.
- **Debounce** — the minimum time between two hits on this band, in milliseconds. Raise it if a band re-fires faster than feels musical (e.g. a kick double-triggering).
- **Range low / Range high (Hz)** — the frequency window this band listens to. Bands may overlap or leave gaps (a gap = nothing reacts to those frequencies). Applied when you press **Save Settings**. Under the inputs, an **"actually hears: ~X–Y Hz"** line shows the truth after the numbers snap to the analysis grid (~23 Hz steps) — and warns in red *before* you save if a range is too narrow to be accepted. If a save has to reject or adjust anything, the save status says exactly what happened and the controls snap back to what was actually saved — the page never silently displays values that didn't take effect. The range feeds **both** outputs: hits and the flow stream.
- **Flow (breathing light) tuning** — a collapsed section per band with the flow stream's personality, each with a plain-language hint under it:
  - **Rise speed** — how fast the light jumps when the band gets louder (high = snappy, low = dreamy).
  - **Fall speed** — how fast it breathes back down (low = long glowing tails, high = tight pulsing).
  - **Contrast** — high = stays near-dark and blooms on peaks; low = gentle mid-brightness wash.
  - **Loudness memory (seconds)** — how long "loud" is remembered: quiet passages look honestly dim for about this long before the brightness range re-expands. The subtlest knob — change it last, judge over a full song.

  All four are per-band and applied live on Save, so the kick flow can be punchy while the melody flow stays smooth.

### The buttons

- **Test flash** — fires one event for that band on demand, so you can confirm the automation and light wiring without needing music.
- **Save Settings** — writes your changes and applies them live.
- **Calibrate Now (6s)** — plays nothing; it just *listens* for six seconds while your music plays, then suggests a starting Sensitivity for each band. Press it, then fine-tune by hand with the meters.

### A good tuning workflow

1. Play a track that's representative of what you usually listen to, at a normal volume.
2. Press **Calibrate Now** to get in the ballpark, then **Apply & Save**.
3. Watch the **Detector** meters. For each band, drag **Sensitivity** so the white line sits just above the resting level but below where real hits spike — the green flash tells you when it's catching them.
4. If a band fires too often on the wrong thing, use the **Spectrum** view to check its **Hz range** — it may be picking up content you don't want (e.g. a bass light that's also catching vocal notes). Narrow the range.
5. If a band re-triggers too fast, raise its **Debounce**.

> **A note on frequency ranges:** the analysis has a resolution of about 23 Hz per step, so a range narrower than that collapses to nothing and won't be accepted. For a bass/kick light, something like **30–110 Hz** captures the whole thump; going too narrow (e.g. 20–35 Hz) starves it and it'll miss kicks.

---

## Protecting your Home Assistant storage

Whatever lights you drive with Trancistor's onset or flow events will change state many times a second while music plays. Home Assistant's `recorder` logs every state change of every entity to its history database by default — so without any changes, these lights alone can generate a steady stream of database writes for as long as music is playing, which adds up over time and can wear out storage (especially SD cards) for a history graph nobody actually needs (a strobing light's history isn't useful to look at).

The Web UI has a card for this — **"Protect your Home Assistant storage"** — that lists your real light entities (fetched live from Home Assistant, including groups: a Zigbee2MQTT or Hue group shows up and works exactly like an individual light, no extra steps). Search and check off whichever lights your Trancistor automations control, and it builds the exact config block for you:

```yaml
recorder:
  exclude:
    entities:
      - light.window
      - light.party_mid_flow

logbook:
  exclude:
    entities:
      - light.window
      - light.party_mid_flow
```

Copy that into your `configuration.yaml` and restart Home Assistant Core for it to take effect. This is purely a generator — the addon never touches your Home Assistant configuration itself, so the actual paste-and-restart is still on you, same as any other config change.

**Does excluding a light from the recorder break anything?** No. The recorder is a passive historian that only feeds the History and Logbook pages — it sits downstream of Home Assistant's live state machine, which is what actually powers dashboard toggles, automations reacting to the light, voice control, and everything else. Excluding a light only stops its (useless, in this case) history graph from being recorded; the light itself keeps working exactly the same.

---

## The events it sends (reference)

For each band there are **two** event types. Use whichever suits the light.

### Onset events (a hit happened) — for flashing lights

| Band | Event type |
|------|-----------|
| Low  | `trancistor_low` |
| Mid  | `trancistor_mid` |
| High | `trancistor_high` |

Event data:

| Field | Meaning |
|-------|---------|
| `hardness` | 0–1: how hard this hit landed *relative to recent hits in this band*. Best for scaling brightness. |
| `strength` | 0–1: raw overall loudness of the moment (all frequencies). |
| `intensity`| `"heavy"` for low, `"soft"` for mid/high (a fixed label you can branch on). |
| `band` | `"low"`, `"mid"`, or `"high"`. |
| `ts` | Timestamp in milliseconds (used for the freshness check in the example automation). |

### Flow events (continuous level) — for breathing lights

| Band | Event type |
|------|-----------|
| Low  | `trancistor_low_level` |
| Mid  | `trancistor_mid_level` |
| High | `trancistor_high_level` |

Event data:

| Field | Meaning |
|-------|---------|
| `brightness` | 0–255, ready to drop straight into `light.turn_on`. Already curved to look right to the eye. |
| `level` | 0–1: the raw level, if you'd rather do your own math. |
| `band` | `"low"`, `"mid"`, or `"high"`. |
| `ts` | Timestamp in milliseconds. |

---

## Advanced tuning (editing the code)

Everything above is adjustable from the Web UI. A handful of deeper behaviors live as clearly-commented constants near the top of `beat_detector.py` — edit them only if the UI knobs aren't enough, and **rebuild the add-on** afterward (Settings → Add-ons → Trancistor → **Rebuild**).

| Constant | What it controls |
|----------|------------------|
| `BANDS` | The default band names and Hz ranges (the Web UI's Hz fields override these once saved). |
| `DEFAULT_BAND_RATIO_THRESHOLDS` | The Sensitivity each band uses until you move its slider. |
| `DECAY_CONFIRM_ENABLED` | Which bands must *confirm* a hit by decaying (default: Low no, Mid/High yes). |
| `SECTION_FLOOR_FRAC` | The quiet-section gate strength per band. Higher = more aggressively ignores events during quiet passages. |
| `LEVEL_MIN_INTERVAL_MS` | How often each band's flow updates are sent, per band (in ms). Lower = smoother, but more commands to your lights. (The flow's rise/fall/contrast/memory live in the Web UI — see the Flow tuning section above.) |
| `LOW_CLICK_RESCUE_ENABLED` | An optional extra kick-catcher that digs out kicks buried under bass by detecting their broadband "click." Off by default — it was designed for a wide low band and does not work on the narrow default one, so don't flip it on without re-testing. |
| `EXPECTED_BEAT_RESCUE_ENABLED` | The quiet-beat expectation window (see above) - on by default, code-side only, no UI toggle. |
| `EXPECT_WINDOW_MS` / `EXPECT_MIN_RATIO` | How wide the on-cue window is (±60 ms) and the minimum peak it will accept (1.45). Widening/lowering catches more quiet beats but risks firing on near-beat noise — both values were tuned against measured failure modes, so move them gently. |

---

## Troubleshooting

**Nothing reacts at all.**
Check the add-on **Log** — do you see beat lines when music plays? If not, the **Audio** input is probably wrong; re-pick your capture device on the Audio tab and restart. Confirm the source actually carries sound.

**A band flashes constantly / on everything.**
Its Sensitivity is too low (white line too far left). Drag it right until only real hits cross it. If it's the *wrong kind* of hit, check the band's Hz range in the Spectrum view.

**A band misses obvious hits.**
Sensitivity too high, or the Hz range is missing the sound's energy — widen the range using the Spectrum view. For kicks specifically, make sure the low band covers roughly 30–110 Hz. Also: a *narrow* low band produces much spikier readings than a wide one, so the right slider position is lower than you'd expect — offline testing found the sweet spot around **1.6–2.2** on the narrow default band, where 2.5+ silently drops a large share of kicks in dense passages.

**Lights lag behind the music / flicker weirdly with many bulbs.**
You're sending commands faster than your lights or network can keep up. Group the bulbs (a Zigbee2MQTT group or a Hue zone) and target the single group entity instead of many individual lights.

**A light strobes harshly and won't fade.**
Some cheap Wi-Fi bulbs ignore fade (`transition`) commands entirely. There's nothing the add-on can do about that — either accept the hard flash, or drive that light with a **flow** event instead of an onset, which sends it a stream of brightness steps that reads more smoothly.

**The whole thing strobes during quiet intros.**
Raise the quiet-section gate for that band (`SECTION_FLOOR_FRAC` in the code), then rebuild.

**The lights all went dead but the add-on looks fine.**
This self-heals: a watchdog inside the add-on notices when audio stops flowing (a wedged capture pipe) and restarts the add-on automatically within about a minute. If you see `FATAL: no audio blocks processed` in the log, that's it doing its job — the restart is intentional.

**A quiet "rescued" flash happens once or twice right after the music stops.**
Expected, and rare: the internal tempo tracker coasts up to ~3 beats before deciding the music really stopped (that's what lets it survive a masked kick mid-song), and the expectation window can act during that brief coast. If it bothers you, lower `PREDICT_LOSE_LOCK_BEATS` in the code — at the cost of losing the lock on every briefly-masked kick.

---

*Trancistor runs entirely on your own hardware. It sends nothing to the internet — it just turns sound into light.*
