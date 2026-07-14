#!/usr/bin/env bash
export PULSE_SERVER=unix:/run/audio/pulse.sock
exec python3 -u /beat_detector.py
