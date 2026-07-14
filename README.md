# Trancistor

A Home Assistant add-on that turns smart lights into a live, music-reactive light show. It listens to a USB audio input, analyzes it in real time (no cloud, no pre-processing), and fires Home Assistant events that you wire up to your own lights with ordinary automations — flashing on the beat, breathing with the melody, or both.

Full explanation of how it works, install steps, ready-to-use automation examples, and a tuning guide are in **[trancistor/DOCS.md](trancistor/DOCS.md)** — that same file is what Home Assistant shows as the add-on's Documentation tab once installed.

## Status: unmaintained, shared as-is

This is a snapshot of a finished personal project. It is **not actively maintained** — no support, no roadmap, no guaranteed response to issues or pull requests. It was built and tested on one setup: a Raspberry Pi (aarch64) running Home Assistant OS, with a USB line-in audio adapter. It should work on similar setups, but that's expected, not verified.

If you find a bug, want a feature, or want to take it somewhere new: **please fork it.** Noncommercial-licensed — free to use, modify, and share for any noncommercial purpose (see License below).

## About this build

This project was vibe coded over a week and a bit in my spare time, using Claude Sonnet 5 and Fable. I'm happy with the performance, so I figured I'd share it.

For my particular setup, I use two WiiM Minis — one feeding my hi-fi system, and the other playing in sync, feeding my Pi 5 through a Cubilux USB SPDIF input adapter. I'm also controlling 5 lights: 3 Zigbee bulbs for the mid flow, one Wi-Fi strip for the low hits, and a Philips Hue bulb for the high hits.

## Installing

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ (top right) → Repositories**.
2. Add this repository's URL: `https://github.com/Iiviin/trancistor`
3. **Trancistor** will appear in the store under your added repositories. Install it, then follow the setup steps in [trancistor/DOCS.md](trancistor/DOCS.md).

## What's in this repo

```
repository.yaml       add-on store metadata (lets HA discover the add-on below)
trancistor/             the add-on itself
  config.json          add-on manifest (name, version, options schema)
  Dockerfile
  run.sh
  beat_detector.py      the detector + web UI (single file, heavily commented)
  DOCS.md               full usage documentation (shown as the add-on's Documentation tab)
```

## License

[PolyForm Noncommercial 1.0.0](LICENSE) — free for any noncommercial purpose (personal use, tinkering, forks, other hobby projects). Not licensed for commercial use — selling it, bundling it into a paid product/service, or similar.
