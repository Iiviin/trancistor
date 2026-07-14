import os, time, json, sys, signal, threading, requests, subprocess
import numpy as np
from collections import deque
import http.server
import socketserver

# ---------------------------------------------------------------------------
# Graceful shutdown - without this, Supervisor's SIGTERM (on stop/restart/
# update) falls through to the OS default disposition, which kills the
# process immediately with exit code 143 instead of a clean 0. Supervisor
# logs that as a warning ("did not handle SIGTERM"). run.sh execs this
# script as PID 1, so the signal lands here directly - just needs a handler.
# ---------------------------------------------------------------------------
shutdown_requested = False

def _handle_shutdown_signal(signum, frame):
    global shutdown_requested
    print(f"Received signal {signum}, shutting down cleanly...", flush=True)
    shutdown_requested = True

signal.signal(signal.SIGTERM, _handle_shutdown_signal)
signal.signal(signal.SIGINT, _handle_shutdown_signal)

# ---------------------------------------------------------------------------
# Options (simplified - see ingress UI for the friendly version of these)
# ---------------------------------------------------------------------------
OPTIONS_PATH = '/data/options.json'

def load_options():
    try:
        with open(OPTIONS_PATH) as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: Could not load {OPTIONS_PATH}, using defaults: {e}", flush=True)
        return {}

opts = load_options()

# ---------------------------------------------------------------------------
# Frequency bands. The Hz edges below are DEFAULTS - the actual ranges are a
# saved setting (band_hz) editable live from the ingress UI, because hardware
# testing showed narrower, deliberately non-contiguous bands work better than
# textbook crossovers: gaps between bands are a feature (content in a gap
# can't trigger anything), not an oversight. All bands run the identical
# pipeline: candidate on a ratio spike, confirm on envelope decay, fire that
# band's own event type. Nothing prioritizes or suppresses anything across
# bands - every band drives its own dedicated light.
# ---------------------------------------------------------------------------
# Each band fires its own distinct HA event TYPE (trancistor_low, ...)
# rather than one shared type distinguished by a data field - this lets each
# band drive its own light with a plain event trigger, no event_data filter.
#
# Default rationale (hardware-tested):
#  - low 25-80Hz: sub/kick fundamentals only. Cutting the 80-250Hz octave
#    (male vocal fundamentals, bass-note harmonics) sharply reduced the
#    vocal-solo misfires the wider 40-250Hz band suffered from.
#  - mid 100-800Hz: typical pop vocal fundamentals - male ~100-400Hz, female
#    ~200-700Hz - excluding outlier whistle notes / sub-bass growls, so the
#    mid melody follower rides the vocal, not cymbals or bass.
#  - high 8-16kHz: cymbal/air only, above vocal sibilance.
BANDS = [
    {"name": "low",  "intensity": "heavy", "low_hz": 25,   "high_hz": 80,    "event_type": "trancistor_low"},
    {"name": "mid",  "intensity": "soft",  "low_hz": 100,  "high_hz": 800,   "event_type": "trancistor_mid"},
    {"name": "high", "intensity": "soft",  "low_hz": 8000, "high_hz": 16000, "event_type": "trancistor_high"},
]
N_BANDS = len(BANDS)

# Ratio-above-threshold alone can't tell "a drum hit" from "a note that just
# started" - both are a sudden energy jump in the same frequency range, and
# the detector has no concept of timbre, only "did energy spike". The one
# reliable acoustic difference is the ENVELOPE: a percussive strike's rise
# above the local baseline collapses again within ~100-200ms, while sustained
# content (a bass note/riff, a sung vowel, a held cymbal wash) stays elevated
# for as long as it lasts. So on EVERY band, a threshold crossing is only a
# CANDIDATE: after up to one grace frame for the attack to finish rising, the
# transient must decay back toward the pre-hit baseline within
# DECAY_CONFIRM_FRAMES to be reported as a hit. Total added latency is
# ~85-128ms, measured as imperceptible on the real lights.
# Per-band: Low fires instantly on the ratio spike, NO envelope confirmation.
# In modern low-end production the "kick" is often an 808: a sharp attack
# welded to a sustained sine tail holding for hundreds of ms - there is
# nothing that decays within any reasonable window, so envelope logic
# rejected real, audible kicks at 98-705% "still elevated" (observed). And
# since the Low band now spans kick AND bass territory (40-250Hz), a sharp
# low attack is a wanted hit regardless of which instrument made it. Mid and
# High keep the filter, where it genuinely separates hits from pads, sung
# vowels and cymbal wash.
DECAY_CONFIRM_ENABLED = [False, True, True]
DECAY_CONFIRM_FRAMES = 2      # frames dedicated purely to checking decay, once the peak is set
DECAY_FACTOR = 0.75           # transient must fall under 75% of its rise above baseline.
                               # Set from real log data: genuine kicks whose decay is partly
                               # masked by a co-occurring deep sustain landed at 63-71%
                               # (wrongly rejected at the old 0.6), while true sustained
                               # content landed at 81-260% - 0.75 sits in the gap between
                               # those two observed populations.
DECAY_RISE_GRACE_FRAMES = 1   # one extra frame for a hit's true peak to fully develop before
                               # decay-checking begins. Fixed, not open-ended: letting it
                               # re-extend while energy kept rising folded neighbouring hits
                               # in dense patterns into one merged candidate (a real bug).

# ---------------------------------------------------------------------------
# Low-band click-confirmed rescue.
#
# The Low band's core conflict: a kick landing on top of sustained bass
# content is only detectable as M/E_sustain - the sustain drags the 25th-pct
# reference up, so real kicks' ratios shrink to ~1.6-2.9 (the truncated
# 2.5-2.9 cluster seen in real logs), while lowering the slider far enough
# to catch them admits bass-note onsets, which within 40-250Hz are
# mathematically identical energy rises. The offline harness refuted every
# in-band separator tried (per-bin spectral flux, bin-spread, sub-envelope
# attack speed - the last is physically impossible: below 250Hz "attack"
# can't be faster than ~1 waveform period). Sidechained EDM is the extreme
# case: the bass swells back to full RIGHT BEFORE each kick, masking
# essentially every kick at any workable slider setting (harness recall:
# 0.04). What DOES separate drums from bass lines is the CLICK: kicks and
# 808s are engineered with a broadband attack transient welded to the low
# rise, while bass notes/swells put almost nothing above 600Hz.
#
# So: a low-band rise that reaches only RESCUE_FRAC of the slider threshold
# still fires IF the same block carries click flux (per-bin half-wave-
# rectified magnitude rise, 600Hz-6kHz) proportional to the low rise itself.
# Strictly additive - everything that fires today still fires; the rescue
# only ADDS sustain-masked kicks. Scaling the click requirement by the low
# rise makes it a timbre test (both sums come from the same event), so it
# is volume-invariant and a quiet hi-hat can't rescue a loud bass note -
# the harness's worst case (bass notes exactly on-grid with 16th hats)
# added ~0 rescue false positives. Known honest gap: a clickless pure-sub
# 808 gains nothing and behaves exactly as today.
#
# 600Hz lower edge keeps bass harmonics out (fundamentals <=~85Hz put
# meaningful partials only below ~500Hz); 6kHz upper edge is where kick
# clicks still carry solid energy.
CLICK_BAND_HZ = (600, 6000)
CLICK_FACTOR = 0.4        # click flux must exceed this fraction of the low rise
RESCUE_FRAC = 0.5         # rescue fires from this fraction of the slider threshold...
RESCUE_MIN_RATIO = 1.55   # ...but never below this ratio: below ~1.5 the low
                          # band is mostly sustain wobble, and the harness
                          # showed swell frames coinciding with hats start
                          # slipping through (12 FPs at a 1.38 floor, 0 at 1.55)

# Master on/off for the whole click-rescue path above. Default OFF: the
# rescue was designed to dig kicks out from under a masking bass sustain on
# a WIDE low band (e.g. 40-250Hz). On the narrow default band above
# (25-80Hz, sub/kick fundamentals only), testing found two things at once:
# the narrow band already cuts most of the bass/vocal content that used to
# get masked, AND the rescue's own mechanism stops working there - a kick's
# sub energy tends to arrive one frame AFTER its broadband click at this
# band width, so the same-block click coincidence the rescue depends on no
# longer reliably occurs (measured click/low_rise ratio near zero at onset
# frames in offline testing). Flip to True only if you widen the Low band
# back out for heavy sidechained EDM, and re-validate the click-flux timing
# first - a same-block click memory and CLICK_FACTOR were tuned for the old
# wide band and are not guaranteed to transfer.
LOW_CLICK_RESCUE_ENABLED = False

# ---------------------------------------------------------------------------
# Internal tempo tracker (Low band). Watches the intervals between REAL Low
# fires, and once the last several agree on a steady tempo, LOCKS and keeps
# a running prediction of the next beat's timing (predict_next_beat). This
# has no user-facing output of its own - it exists purely to power the
# expectation-window rescue below. (An earlier version also emitted a
# separate "predicted beat" HA event for lag-compensated flashing; that was
# cut as not worth the added complexity, but this tracker stayed since the
# rescue depends on it.) Always on - no enable/disable setting.
#
# Locks once >= PREDICT_MIN_AGREE of the last PREDICT_HISTORY intervals
# agree within +/-PREDICT_TOL of their median; the lock drops after
# PREDICT_LOSE_LOCK_BEATS expected beats pass with no real fire (breakdown,
# tempo change, song end). Known honest limits: needs ~5 steady kicks to
# lock (a fill or two skipped beats is bridged by coasting, a real tempo
# change relocks in ~5 beats).
# ---------------------------------------------------------------------------
PREDICT_MIN_IOI_MS = 250.0       # 240 BPM ceiling...
PREDICT_MAX_IOI_MS = 1000.0      # ...60 BPM floor for a plausible kick pulse
PREDICT_HISTORY = 8              # intervals kept for the tempo vote
PREDICT_MIN_AGREE = 5            # this many must agree to (stay) locked
PREDICT_TOL = 0.12               # "agree" = within +/-12% of the median
PREDICT_LOSE_LOCK_BEATS = 3.5    # coast at most this many expected beats with
                                 # no real fire. 3.5 (not 2.5) so TWO
                                 # consecutive masked kicks - a 3-interval gap
                                 # between real fires - still coast instead of
                                 # unlocking (sim-verified); cost is at most ~3
                                 # beats of stale lock before it drops.

# ---------------------------------------------------------------------------
# Expectation-window rescue (tempo-gated, Low band) - validated offline
# before shipping. The problem it solves: beats you can clearly HEAR during quieter
# passages that the lights don't flash - the kick is still on the
# grid, just small enough that the section gate (deliberately conservative)
# or the slider blocks it. While the tempo tracker above is LOCKED, inside
# +/-EXPECT_WINDOW_MS of each predicted beat the bar drops: ratio only needs
# max(EXPECT_MIN_RATIO, EXPECT_FRAC*slider) and the section floor relaxes to
# EXPECT_SECTION_RELAX of itself. Off the grid, nothing changes - an equal-
# size peak between beats never sees the lowered bar. Tempo phase is the one
# signal that can tell "quiet but on cue" (a real beat) from "quiet wobble"
# (noise), which is exactly what the section gate alone cannot do.
#
# Measured in the harness: quiet on-grid kicks under the section gate went
# 0/16 -> 16/16 recovered with ZERO fires on equal-size off-grid distractors
# (rescued fires also re-claim the debounce window, actively suppressing
# them). Full-battery cost is a wash. Known honest limits: needs the lock
# (no help before ~5 steady beats); rumble-buried kicks barely improve
# (a rumble peak IS an in-band rise - same masking wall as ever); a
# periodic BASSLINE that already fires the detector will get a few more
# rescued fires (in-band, a periodic bass pulse is indistinguishable from a
# kick - that trade-off is this addon's oldest settled physics).
# EXPECT_MAX_CONSECUTIVE caps rescued-only beats so noise can't keep a
# stale lock alive forever: after this many in a row, rescuing pauses until
# a full-threshold fire re-arms it. Fires are tagged "(expected)" in the
# log and carry "expected": true in the event payload - and their hardness
# is naturally small, so hardness-scaled automations dim them for free.
# ---------------------------------------------------------------------------
EXPECTED_BEAT_RESCUE_ENABLED = True
EXPECT_WINDOW_MS = 60.0     # +/- around the predicted beat. NOT wider: at 80ms
                            # the harness measured early near-beat noise peaks
                            # firing AND stealing the debounce from the real
                            # kick right behind them.
EXPECT_MIN_RATIO = 1.45     # rescue ratio floor - added FPs measured at 80ms/
                            # 1.25 lived in the 1.25-1.8 band; on-cue quiet
                            # kicks measure far above this against a quiet floor
EXPECT_FRAC = 0.55          # ...or this fraction of the slider, whichever is more
EXPECT_SECTION_RELAX = 0.3  # section floor multiplier inside the window
EXPECT_MAX_CONSECUTIVE = 16 # rescued-only beats allowed before re-arm is needed

# Continuous level follower - alongside the discrete onset pipeline, EVERY
# band also emits a continuous level signal (trancistor_<band>_level) so its
# light can ride that band's intensity - breathing/pulsing - rather than only
# flash on hits. Envelope follower with fast attack / slower release (jumps
# with swells, breathes out with them), normalized against a slowly-decaying
# rolling peak of that band itself - same everything-is-relative philosophy as
# the rest of the pipeline, so it self-adapts to track loudness with no gain
# knob. A band's flow output and its onset output are independent - wire a
# light to whichever (or both) in the HA automation. Especially useful for
# lights that can't fade (e.g. some inexpensive WiFi bulbs/strips ignore the
# `transition` field entirely): the stepped level stream reads far smoother
# than a hard on/off onset strobe on hardware like that.
#
# The flow's per-band "personality" - rise/fall/contrast/memory - is a SAVED
# SETTING (flow_attack / flow_release / flow_gamma / flow_memory_s in
# DEFAULTS below), tunable live from the collapsible Flow section in the UI.
# Defaults: attack 0.5 (per-frame smoothing while rising), release 0.2
# (~140ms to fall halfway, ~600ms to effectively off - a value that reads
# well on real hardware without lingering too long after the sound drops),
# gamma 2.2 (level**gamma brightness curve: quiet levels compress hard
# toward 0 instead of idling visibly lit), memory 30s (the rolling-peak
# half-life the level is normalized against - how long a quiet passage
# looks quiet, tunable in honest seconds). apply_flow_settings() below
# converts them to the per-frame arrays the audio thread actually reads.
LEVEL_MIN_INTERVAL_MS = [200.0, 200.0, 400.0]  # min gap between level sends, PER BAND.
                                     # Low=200ms (5Hz): resolves kick AND offbeat at every
                                     # EDM tempo (4-on-floor kick ~470ms @128BPM, offbeat
                                     # ~235ms) - a good default for a single light with no
                                     # command fan-out. Mid/High=200/400ms: if a band drives
                                     # SEVERAL lights, each flow update becomes one command
                                     # PER light unless they're grouped at the source (e.g. a
                                     # Zigbee2MQTT group or a Hue zone/room) - group first,
                                     # then lower the interval; pushing an ungrouped band much
                                     # faster than its default risks flooding your mesh/hub.
                                     # If lights start visibly LAGGING the music (not just
                                     # skipping the occasional update), raise the interval back.
LEVEL_MIN_DELTA = 0.08               # and only when the level moved at least this much
LEVEL_EVENT_TYPES = [f"trancistor_{b['name']}_level" for b in BANDS]

# Used only when a band hasn't been calibrated/tuned yet (threshold 0 in
# options). Thresholds are a ratio of "current energy" to a SHORT (~213ms)
# trailing local average for this band - e.g. 2.5 means "2.5x louder than
# this band sounded a moment ago". Comparing against a short window (not a
# slow multi-second baseline) is what lets a real kick still register in a
# busy mix, AND avoids over-firing during a song's gradual intensity build-up
# or breakdown: a smooth multi-second crescendo barely changes over 213ms, so
# it never produces a big ratio against its own recent past - only a genuine
# sub-100ms transient (an actual hit) does, regardless of the overall trend.
DEFAULT_BAND_RATIO_THRESHOLDS = [2.5, 2.2, 2.0]

# Absolute floor per band: guards against the ratio math blowing up on tiny
# noise during near-silent passages, where dividing by a near-zero recent
# average can turn an insignificant blip into a huge (but meaningless) ratio.
# Scaled per-band by FFT bin count (computed below, once BAND_BINS exists) -
# low spans ~9 bins while high spans ~500, so raw summed energy is naturally
# far larger for high at the same perceived loudness. A single flat floor
# would guard the narrow band but be a no-op for the wide ones.
MIN_ENERGY_PER_BIN = 0.5 / 3  # per-bin floor originally validated on the old
                              # 3-bin kick band; same order of magnitude here

# Section-loudness gate. The absolute floor above is a single fixed number tuned once -
# it can't tell "the whole track just went quiet" from "this band is always
# this size", so during a quiet passage that follows a loud one, a real hit
# and a harmless wobble in the noise floor can both clear it, and the ratio
# check (scale-invariant by design) can't tell them apart either: a tiny
# absolute rise against an equally tiny recent reference still produces a
# big ratio. The harness reproduced this directly - a hushed ambient pad
# under a "quiet section" produced 21 false Low triggers with the gate off.
#
# Fix: each band remembers a slow-decaying peak of its own absolute energy
# (same fast-rise/slow-decay pattern already used for the level follower's
# level_peak_ref, just for gating instead of level normalization).
# A hit must clear a floor that's a fraction of that peak, not just the
# fixed MIN_ENERGY_PER_BIN one. Because the peak has real memory (~15s
# half-life), it stays elevated through a breakdown/quiet intro that
# follows a loud section - damping ratio-noise there - while a genuine
# quieter hit (still a healthy fraction of "how loud this band recently
# got") clears it fine. Per-band, not whole-track: keeps the same units as
# each band's own absolute floor, no cross-band rescaling needed.
#
# Per-band, harness-measured headroom differs a lot: Low was clean (zero
# false positives, zero recall cost) across the full 0.08-0.35 range
# tested. High is far more sensitive - dense content (16th-note hi-hats)
# loses meaningful recall even at 0.08, because with hits closer together
# than the ~128ms decay-confirm window, overlapping decay tails can
# inflate the peak reference above what a single hit deserves. So Low gets
# a comfortable value; High gets a conservative one. Mid is left disabled
# (0) - it wasn't part of the reported problem and wasn't validated here.
# Known open gap the gate does NOT fix: a real kick's broadband click
# bleeding into 4-16kHz still reads as a plausible quiet High hit
# regardless of this fraction - that's cross-band bleed, a different
# mechanism from "the track got quiet", and needs a different fix if it
# turns out to be significant on hardware.
SECTION_FLOOR_FRAC = [0.25, 0.0, 0.08]     # low, mid, high; 0 disables a band
                                           # Low=0.25: the harness showed headroom up to
                                           # 0.35 with zero measured recall cost, and real
                                           # hardware testing still showed kicks
                                           # over-triggering in quiet sections at lower
                                           # values - 0.25 sits partway into that
                                           # validated-safe range, leaving room to push
                                           # further if your hardware still strobes.
SECTION_PEAK_DECAY = 0.998                 # per-frame decay, ~15s half-life

# Debounce is a real, separate control from the threshold sliders: raising a
# band's threshold makes it need a louder relative spike to fire at all, but
# does nothing if a band is legitimately loud enough *and* still re-triggering
# faster than feels musical. Exposed per-band (not one global number) since a
# hi-hat pattern and a kick pattern have very different natural repeat rates.
DEFAULT_LOCKOUT_MS = [320.0, 320.0, 320.0]

DEFAULTS = {
    "band_thresholds": [0.0, 0.0, 0.0],          # 0 = untuned, use default above
    "band_lockout_ms": DEFAULT_LOCKOUT_MS,       # min time between two hits on the SAME band
    "band_enabled": [True, True, True],          # explicit per-band on/off, separate from
                                                  # the threshold slider's "max = mute" side effect
    # Flat [lo0, hi0, lo1, hi1, lo2, hi2] rather than nested pairs, because
    # the HA addon options schema validates flat typed lists cleanly.
    "band_hz": [float(v) for b in BANDS for v in (b["low_hz"], b["high_hz"])],
    # Per-band flow personality (see the level-follower comment above for
    # what each one does and where the defaults came from).
    "flow_attack": [0.5, 0.5, 0.5],
    "flow_release": [0.2, 0.2, 0.2],
    "flow_gamma": [2.2, 2.2, 2.2],
    "flow_memory_s": [30.0, 30.0, 30.0],
}

# Clamp ranges for the flow settings, shared by the sanitizer. Attack/release
# are per-frame smoothing alphas (1.0 = instant); gamma below 0.5 or above
# 5.0 stops looking like a curve and starts looking like a bug; memory under
# ~3s makes the normalization pump audibly with individual hits.
FLOW_CLAMPS = (("flow_attack", 0.05, 1.0), ("flow_release", 0.02, 1.0),
               ("flow_gamma", 0.5, 5.0), ("flow_memory_s", 3.0, 180.0))

# Internal fixed timing - not exposed, these rarely need tuning.
SHORT_HISTORY_LEN = 5                # ~213ms trailing window at this blocksize/rate
SHORT_WINDOW_PERCENTILE = 25         # low percentile (not median/50th) so the reference stays
                                      # anchored near baseline even during dense/busy passages -
                                      # see the comment at its use in process_block for why
PEAK_DECAY = 0.985                   # UI meter peak-hold decay per frame (~2s to settle)
RATIO_CAP = 20.0                     # hard ceiling on the ratio itself, so a hit right after
                                      # near-silence can't produce a meaningless 100s-1000s
                                      # value that pins the UI meter near max for many seconds
                                      # as it slowly decays back down

# Ceiling a calibration suggestion can report, and the UI slider's own max
# (see SLIDER_MAX in the page JS below) - kept equal to RATIO_CAP so the
# slider can reach anywhere the ratio itself can actually go. This used to be
# a separate, lower number (8), which meant real hit ratios on spiky bands
# (hats/cymbals routinely land in the 10-16 range) could never be matched by
# any slider position - the UI's own "drag to just above the peak" guidance
# was impossible to follow once a band's real peaks exceeded the old ceiling.
#
# Deliberate consequence: because the ratio is capped AT this value and the
# hit check is a strict ">", a threshold slider dragged all the way to max
# can never fire - that position is the explicit "mute this band" setting,
# and the UI labels it as such. Calibration suggestions are capped just
# below it so an Apply & Save can never silently mute a band.
THRESHOLD_SLIDER_MAX = RATIO_CAP

SAMPLE_RATE = 48000
BLOCKSIZE = 2048
BIN_HZ = SAMPLE_RATE / BLOCKSIZE  # ~23.4Hz per FFT bin at this blocksize/rate
HANN_WINDOW = np.hanning(BLOCKSIZE).astype(np.float32)

settings_lock = threading.Lock()

TOKEN = os.environ.get("SUPERVISOR_TOKEN")
if not TOKEN:
    print("ERROR: SUPERVISOR_TOKEN missing.", flush=True)
    sys.exit(1)

HA_EVENTS_URL_TEMPLATE = "http://supervisor/core/api/events/{event_type}"
HA_STATES_URL = "http://supervisor/core/api/states"
SUPERVISOR_OPTIONS_URL = "http://supervisor/addons/self/options"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

def hz_to_bin(hz):
    return max(0, int(round(hz / BIN_HZ)))

def apply_band_hz(band_hz):
    # Applies a flat [lo0, hi0, lo1, hi1, lo2, hi2] Hz list to BANDS and
    # rebuilds everything derived from it. New list objects are built first
    # and swapped in as whole references, so the audio thread (which reads
    # BAND_BINS/MIN_BAND_ENERGY mid-frame) always sees a complete, matched
    # set - never a half-updated one.
    global BAND_BINS, MIN_BAND_ENERGY
    for i, b in enumerate(BANDS):
        b["low_hz"], b["high_hz"] = band_hz[2 * i], band_hz[2 * i + 1]
    BAND_BINS = [(hz_to_bin(b["low_hz"]), hz_to_bin(b["high_hz"])) for b in BANDS]
    MIN_BAND_ENERGY = [(hi - lo) * MIN_ENERGY_PER_BIN for lo, hi in BAND_BINS]

apply_band_hz(DEFAULTS["band_hz"])
CLICK_BINS = (hz_to_bin(CLICK_BAND_HZ[0]), hz_to_bin(CLICK_BAND_HZ[1]))

FRAMES_PER_SEC = SAMPLE_RATE / BLOCKSIZE  # ~23.4 - for seconds -> per-frame decay

def apply_flow_settings(s):
    # Converts the human-facing flow settings into the per-frame arrays the
    # audio thread reads. Whole-reference swaps (same pattern as
    # apply_band_hz) so a frame never sees a half-updated set. Memory is
    # stored in honest seconds and converted to a per-frame decay here:
    # decay = 0.5 ** (1 / (seconds * frames_per_sec)) gives exactly that
    # half-life (30s -> the old hardcoded 0.999).
    global FLOW_ATTACK, FLOW_RELEASE, FLOW_GAMMA, FLOW_DECAY
    FLOW_ATTACK = np.array(s["flow_attack"], dtype=np.float64)
    FLOW_RELEASE = np.array(s["flow_release"], dtype=np.float64)
    FLOW_GAMMA = list(s["flow_gamma"])
    FLOW_DECAY = np.array([0.5 ** (1.0 / (max(3.0, m) * FRAMES_PER_SEC))
                           for m in s["flow_memory_s"]], dtype=np.float64)

apply_flow_settings(DEFAULTS)

# Spectrum display bars for the UI visualizer: the full FFT (1025 bins) is
# folded into N log-spaced bars covering 20Hz-20kHz, so an octave of bass
# gets the same visual width as an octave of treble (matching both hearing
# and how the bands are laid out). MEAN per bar, not sum - a sum would make
# the wide high-frequency bars tower over the narrow low ones purely because
# they contain more bins, not because they're louder. Edges are forced
# strictly increasing (the bottom bars would otherwise all round to the same
# 1-2 FFT bins), which makes the lowest bars effectively linear-spaced -
# standard analyzer behavior. Display-only: detection never reads these.
N_SPECTRUM_BARS = 96
_spec_edges = np.round(np.logspace(np.log10(20.0), np.log10(20000.0),
                                   N_SPECTRUM_BARS + 1) / BIN_HZ).astype(int)
_spec_edges = np.maximum(_spec_edges, 1)  # never include bin 0 (DC offset)
for _k in range(1, len(_spec_edges)):
    _spec_edges[_k] = max(_spec_edges[_k], _spec_edges[_k - 1] + 1)
SPECTRUM_EDGES = _spec_edges
SPECTRUM_COUNTS = np.diff(SPECTRUM_EDGES).astype(np.float32)

def effective_thresholds(s):
    cfg = s["band_thresholds"]
    return [
        cfg[i] if i < len(cfg) and cfg[i] > 0 else DEFAULT_BAND_RATIO_THRESHOLDS[i]
        for i in range(N_BANDS)
    ]

def sanitize_settings(payload, current, warnings=None):
    # Runs on BOTH input paths: /save POSTs and the boot-time options.json
    # load. A malformed band_thresholds or band_lockout_ms (wrong type or
    # length) reaches process_block() and throws there, which the capture
    # loop's except then treats as a stream failure - restarting parec every
    # single frame in a tight crash loop until the bad value is fixed.
    # Falling back to the current value for anything that fails to parse
    # keeps bad input from ever reaching that.
    #
    # `warnings`, when given, collects a human-readable line for every field
    # that was REJECTED and fell back. This exists because silent fallback is
    # a real trap: a too-narrow band range could be quietly kept at its old
    # value while the UI said "Saved." and displayed the rejected numbers.
    # /save forwards these to the page; boot prints them to the log.
    #
    # List values are copied, not aliased: at boot `current` is DEFAULTS
    # itself, and an in-place mutation through settings would otherwise
    # corrupt the defaults for the rest of the process's lifetime.
    if warnings is None:
        warnings = []
    result = {k: (list(v) if isinstance(v, list) else v) for k, v in current.items()}

    try:
        bt = [max(0.0, min(float(v), THRESHOLD_SLIDER_MAX)) for v in payload["band_thresholds"]]
        if len(bt) == N_BANDS:
            result["band_thresholds"] = bt
        else:
            warnings.append("band_thresholds ignored (wrong length)")
    except KeyError:
        pass
    except (TypeError, ValueError):
        warnings.append("band_thresholds ignored (not numbers)")

    try:
        lm = [max(20.0, min(float(v), 3000.0)) for v in payload["band_lockout_ms"]]
        if len(lm) == N_BANDS:
            result["band_lockout_ms"] = lm
        else:
            warnings.append("band_lockout_ms ignored (wrong length)")
    except KeyError:
        pass
    except (TypeError, ValueError):
        warnings.append("band_lockout_ms ignored (not numbers)")

    try:
        be = [bool(v) for v in payload["band_enabled"]]
        if len(be) == N_BANDS:
            result["band_enabled"] = be
        else:
            warnings.append("band_enabled ignored (wrong length)")
    except KeyError:
        pass
    except (TypeError, ValueError):
        warnings.append("band_enabled ignored (bad values)")

    try:
        hz = [max(20.0, min(float(v), 20000.0)) for v in payload["band_hz"]]
        if len(hz) == 2 * N_BANDS:
            # Validate per pair, falling back per pair: each band must be
            # ascending AND wide enough to cover at least one FFT bin
            # (~23.4Hz), or its energy sum would be a constant 0 and the
            # band would silently go dead.
            cur = result["band_hz"]
            for i in range(N_BANDS):
                lo, hi = hz[2 * i], hz[2 * i + 1]
                if hi > lo and hz_to_bin(hi) > hz_to_bin(lo):
                    cur[2 * i], cur[2 * i + 1] = lo, hi
                elif (lo, hi) != (cur[2 * i], cur[2 * i + 1]):
                    warnings.append(
                        f"{BANDS[i]['name']} range {lo:.0f}-{hi:.0f}Hz rejected - too narrow "
                        f"(must span at least one ~{BIN_HZ:.0f}Hz FFT bin); kept "
                        f"{cur[2 * i]:.0f}-{cur[2 * i + 1]:.0f}Hz")
        else:
            warnings.append("band_hz ignored (wrong length)")
    except KeyError:
        pass
    except (TypeError, ValueError):
        warnings.append("band_hz ignored (not numbers)")

    for key, lo_clamp, hi_clamp in FLOW_CLAMPS:
        try:
            vals = [max(lo_clamp, min(float(v), hi_clamp)) for v in payload[key]]
            if len(vals) == N_BANDS:
                result[key] = vals
            else:
                warnings.append(f"{key} ignored (wrong length)")
        except KeyError:
            pass
        except (TypeError, ValueError):
            warnings.append(f"{key} ignored (not numbers)")

    return result

# Live, mutable settings - loaded from options.json, editable at runtime via
# the ingress UI (Save pushes changes here immediately AND persists them).
# Boot-time options go through the same sanitizer as /save: the HA config
# panel's schema validates element TYPES but not list LENGTHS, so an
# options.json edited down to e.g. a 3-element band_lockout_ms would
# otherwise IndexError inside the audio callback and crash-loop the capture.
_boot_warnings = []
settings = sanitize_settings(opts, DEFAULTS, _boot_warnings)
for _w in _boot_warnings:
    print(f"options.json: {_w}", flush=True)
apply_band_hz(settings["band_hz"])
apply_flow_settings(settings)

# ---------------------------------------------------------------------------
# Event dispatch - onsets and flow levels have different delivery needs, so
# they don't share one FIFO. The old single Queue(maxsize=5) with drop-oldest
# meant a burst of flow chatter arriving while HA was slow could evict a KICK
# event - the one payload where every single occurrence matters. Now:
#
#  - Onset ("beat") events go in a bounded deque; nothing else can push them
#    out. If HA is so slow that even this overflows, the oldest onset drops -
#    it would have failed the automations' 300ms freshness check anyway.
#  - Flow ("level") events coalesce per event type: only the LATEST level per
#    band is ever pending. That's the correct semantics for a level signal -
#    intermediate values a slow consumer missed are worthless by definition -
#    and it means flow can never occupy more than 3 slots no matter how fast
#    it's produced.
#
# Workers always drain onsets before touching flow.
# ---------------------------------------------------------------------------
event_cond = threading.Condition()
onset_events = deque()          # (event_type, data), bounded manually below
flow_pending = {}               # event_type -> latest data (coalesced)
ONSET_QUEUE_MAX = 16

def enqueue_event(event_type, data):
    with event_cond:
        if data.get("type") == "beat":
            if len(onset_events) >= ONSET_QUEUE_MAX:
                onset_events.popleft()
            onset_events.append((event_type, data))
        else:
            flow_pending[event_type] = data
        event_cond.notify()

def event_worker():
    session = requests.Session()
    while True:
        with event_cond:
            while not onset_events and not flow_pending:
                event_cond.wait()
            if onset_events:
                event_type, data = onset_events.popleft()
            else:
                event_type, data = flow_pending.popitem()
        url = HA_EVENTS_URL_TEMPLATE.format(event_type=event_type)
        try:
            session.post(url, headers=HEADERS, json=data, timeout=0.5)
        except Exception:
            pass

# 3 workers so one slow HA API call can't delay the next beat. Trade-off:
# two workers can race, so events may reach HA slightly out of order - fine
# here, since the kick and soft automations are independent of each other.
for _ in range(3):
    threading.Thread(target=event_worker, daemon=True).start()

# ---------------------------------------------------------------------------
# Live state for meters + calibration (shared between audio thread and web UI)
# ---------------------------------------------------------------------------
live_lock = threading.Lock()
live_state = {"band_energy": [0.0] * N_BANDS, "band_ratio": [0.0] * N_BANDS,
              "band_peak": [1.0] * N_BANDS, "ts": 0,
              "spectrum": [0.0] * N_SPECTRUM_BARS,
              "band_level": [0.0] * N_BANDS,
              "band_last_hit": [0.0] * N_BANDS}

calibration_lock = threading.Lock()
calibration_state = {"running": False, "result": None, "music_frames": 0, "total_frames": 0}
CALIBRATE_DURATION_S = 6.0
calibration_samples = {b["name"]: [] for b in BANDS}

# ---------------------------------------------------------------------------
# History / lockout state
# ---------------------------------------------------------------------------
# Lockout timestamps are MONOTONIC milliseconds (see process_block for why),
# initialized far in the past so the first hit after boot is never blocked -
# unlike wall time, time.monotonic() starts near zero.
LONG_HISTORY_LEN = 150
long_history = np.full(LONG_HISTORY_LEN, 0.001)
long_idx = 0

# Short trailing window per band, used as the onset reference (see
# DEFAULT_BAND_RATIO_THRESHOLDS comment above for why short, not slow-EMA).
short_history = np.full((N_BANDS, SHORT_HISTORY_LEN), 0.001)
short_idx = 0

peak_display = np.ones(N_BANDS)
last_band_fire = np.full(N_BANDS, -1e9)  # per-band lockout timestamps (monotonic ms)

# Section-loudness gate state (see SECTION_FLOOR_FRAC): slow-decaying peak
# of each band's own absolute energy, remembering "how loud this band
# recently got" across a quiet passage.
section_peak = np.full(N_BANDS, 1e-6)

# Pending decay-confirmation candidates, one slot per band. Each candidate
# passes through two phases: grace (peak may still be rising) then
# decay-check (fixed length, never updates the peak). pending_base holds the
# band's pre-hit reference level at candidate start - decay is judged on the
# rise ABOVE that baseline, not on absolute energy (see process_block).
pending_active = [False] * N_BANDS
pending_in_grace = [False] * N_BANDS
pending_peak = [0.0] * N_BANDS
pending_base = [0.0] * N_BANDS
pending_frames_left = [0] * N_BANDS
pending_ratio = [0.0] * N_BANDS
pending_strength = [0.0] * N_BANDS
pending_ts = [0.0] * N_BANDS

# Previous frame's click-band magnitudes (600Hz-6kHz), for the Low band's
# click-confirmed rescue. None until the first frame has been seen.
prev_click_mag = None

# Per-band continuous level follower state (see the LEVEL_* constants above).
level_env = np.zeros(N_BANDS)
level_peak_ref = np.full(N_BANDS, 1e-6)
level_sent = [-1.0] * N_BANDS
level_ts = [-1e9] * N_BANDS

# Tempo tracker state (see the PREDICT_* constants above). All times are
# MONOTONIC ms.
predict_onsets = deque(maxlen=PREDICT_HISTORY + 1)  # recent real Low fires
predict_locked = False
predict_interval = 0.0        # locked inter-onset interval estimate (ms)
predict_next_beat = 0.0       # monotonic ms of the next expected beat
expect_consecutive = 0        # rescued-only beats in a row (EXPECT_MAX_CONSECUTIVE)

callback_count = 0
last_heartbeat = time.monotonic()
# Liveness timestamp for the audio-stall watchdog thread (see
# audio_stall_watchdog below): bumped on every processed block.
last_block_mono = time.monotonic()

def _predict_on_low_fire(now_mono):
    # Called on every REAL Low fire (never test flashes). Re-votes the tempo
    # from the last PREDICT_HISTORY inter-onset intervals and re-anchors the
    # next expected beat to THIS fire - so prediction phase continuously
    # corrects itself against reality and can never drift more than one
    # beat from the real music.
    global predict_locked, predict_interval, predict_next_beat
    predict_onsets.append(now_mono)
    times = list(predict_onsets)
    ivs = [b - a for a, b in zip(times, times[1:])]
    ivs = [iv for iv in ivs if PREDICT_MIN_IOI_MS <= iv <= PREDICT_MAX_IOI_MS]
    if len(ivs) >= PREDICT_MIN_AGREE:
        med = float(np.median(ivs))
        agree = [iv for iv in ivs if abs(iv - med) <= PREDICT_TOL * med]
        if len(agree) >= PREDICT_MIN_AGREE:
            predict_interval = float(np.mean(agree))
            if not predict_locked:
                print(f"PREDICT: locked at {60000.0 / predict_interval:.1f} BPM "
                      f"(interval {predict_interval:.0f}ms)", flush=True)
            predict_locked = True
            predict_next_beat = now_mono + predict_interval
            return
    # Vote failed (fill, syncopation, tempo drifting): if locked, keep the
    # old interval and just re-anchor phase to this fire - the lose-lock
    # logic in process_block handles genuine loss.
    if predict_locked:
        predict_next_beat = now_mono + predict_interval

# ---------------------------------------------------------------------------
# Per-block audio processing (called by the capture loop for every block)
# ---------------------------------------------------------------------------
def process_block(block):
    global long_idx, callback_count, last_heartbeat
    global short_idx, peak_display, last_band_fire, prev_click_mag
    global section_peak
    global level_env, level_peak_ref, level_sent, level_ts
    global pending_active, pending_in_grace, pending_peak, pending_frames_left
    global pending_base, pending_ratio, pending_strength, pending_ts
    global last_block_mono
    global predict_locked, predict_next_beat
    global expect_consecutive

    last_block_mono = time.monotonic()

    # Silent health check (replaces the old every-5s "heartbeat" log spam):
    # count blocks over a 30s window and only speak up if we're processing
    # meaningfully SLOWER than realtime (~23.4 blocks/s at this rate/blocksize),
    # which is the one thing worth knowing - the Pi falling behind the audio,
    # causing dropped/laggy detection. A healthy stream now logs nothing.
    callback_count += 1
    elapsed = time.monotonic() - last_heartbeat
    if elapsed > 30:
        expected = elapsed * SAMPLE_RATE / BLOCKSIZE
        if callback_count < 0.8 * expected:
            print(f"WARNING: audio processing behind realtime - {callback_count} blocks "
                  f"in {elapsed:.0f}s (expected ~{expected:.0f})", flush=True)
        callback_count = 0
        last_heartbeat = time.monotonic()

    mono = np.mean(block, axis=1).astype(np.float32)

    # Two clocks, deliberately: wall time goes into event payloads and logs
    # (the HA automation compares event ts against its own wall clock), while
    # monotonic time drives ALL internal timing - lockouts and cooldowns. An
    # NTP step backwards would otherwise make "now - last_fire" negative and
    # silently mute every band until the wall clock caught back up; a forward
    # step would instantly expire lockouts that should still be active.
    now_wall = time.time() * 1000
    now_mono = time.monotonic() * 1000

    # Hann window before the FFT reduces spectral leakage between bands.
    fft_data = np.abs(np.fft.rfft(mono * HANN_WINDOW))
    band_energy = np.array([float(np.sum(fft_data[lo:hi])) for lo, hi in BAND_BINS])

    # Ratio of "right now" to "this band a moment ago" (short_history is only
    # ~213ms of trailing frames). Deliberately NOT a slow multi-second
    # baseline: a song's gradual intensity build-up or breakdown moves slowly
    # enough that it barely changes over 213ms, so it never produces a big
    # ratio here - only an actual sub-100ms transient (a real hit) does,
    # regardless of whether the overall trend is rising or falling.
    #
    # A LOW PERCENTILE (not mean, not median): a mean lets one huge sample
    # (the hit itself) dominate a short window, so a few frames later - as
    # that sample rolls back out of the window - the average suddenly drops
    # and the hit's own decay tail looks like a *fresh* onset relative to the
    # newly-lowered average. That produced a real bug: one loud kick
    # "echoing" into a rapid-fire cascade of several fake follow-up kicks.
    #
    # The median (50th percentile) fixed that, but has its own failure mode:
    # median needs a MAJORITY of the window (3 of 5 samples) to be quiet to
    # stay anchored near the true baseline. During a busy/dense passage
    # (e.g. a drop with kicks landing close together), more and more of that
    # window is itself kick energy rather than quiet - once 3+ of 5 samples
    # are elevated, the median starts reflecting "typical busy level" instead
    # of "baseline", so each subsequent kick's ratio shrinks a little more
    # against a rising reference until it quietly drops below threshold -
    # looking like the detector "gets used to" a beat drop and stops firing
    # even though the hits are still happening. A lower percentile (25th)
    # only needs about 1 of 5 samples to be genuinely quiet to stay anchored
    # near the floor - far more resistant to being dragged up by a dense run
    # of hits, while rejecting a single loud outlier at least as well as the
    # median did.
    short_avg = np.percentile(short_history, SHORT_WINDOW_PERCENTILE, axis=1)
    ratio = np.minimum(band_energy / np.maximum(short_avg, 1e-6), RATIO_CAP)

    # Settings snapshot for this frame - read once, used by both the flow
    # emission below (band_enabled) and the onset pipeline further down.
    with settings_lock:
        s = dict(settings)
    thresholds = effective_thresholds(s)
    band_lockout_ms = s["band_lockout_ms"]
    band_enabled = s["band_enabled"]

    # Per-band continuous level (breathing/pulsing), alongside the discrete
    # onset events. Vectorized envelope follower: fast attack, slower release,
    # normalized against each band's own slowly-decaying rolling peak. The
    # FLOW_* arrays are per-band and user-tunable (see apply_flow_settings).
    rising = band_energy > level_env
    alpha = np.where(rising, FLOW_ATTACK, FLOW_RELEASE)
    level_env += alpha * (band_energy - level_env)
    level_peak_ref = np.maximum(band_energy, level_peak_ref * FLOW_DECAY)
    level = np.minimum(1.0, level_env / np.maximum(level_peak_ref, 1e-6))

    # UI spectrum: reduceat sums each [edge[i], edge[i+1]) bin run in one
    # vectorized pass; the trailing element (edge[-1] to end of array) is
    # dropped. Mean per bar - see the SPECTRUM_EDGES comment.
    spectrum = np.add.reduceat(fft_data, SPECTRUM_EDGES)[:-1] / SPECTRUM_COUNTS

    with live_lock:
        peak_display = np.maximum(ratio, peak_display * PEAK_DECAY)
        live_state["band_energy"] = band_energy.tolist()
        live_state["band_ratio"] = ratio.tolist()
        live_state["band_peak"] = peak_display.tolist()
        live_state["band_level"] = np.round(level, 3).tolist()
        live_state["spectrum"] = np.round(spectrum, 2).tolist()
        live_state["ts"] = now_wall

    # Throttled per-band level emission: at most one update per MIN_INTERVAL
    # per band, and only when that band's level actually moved. Slow drift
    # still gets through because the comparison is against the last SENT
    # level - small changes accumulate until they cross the delta.
    # A DISABLED band emits no flow - except one final brightness-0 event at
    # the moment of disabling, so a listening light fades out rather than
    # freezing at whatever brightness it happened to be showing.
    for i in range(N_BANDS):
        if not band_enabled[i]:
            if level_sent[i] != 0.0:
                level_ts[i] = now_mono
                level_sent[i] = 0.0
                enqueue_event(LEVEL_EVENT_TYPES[i],
                              {"type": "level", "band": BANDS[i]["name"], "level": 0.0,
                               "brightness": 0, "ts": now_wall})
            continue
        lvl = float(level[i])
        if (now_mono - level_ts[i] > LEVEL_MIN_INTERVAL_MS[i]
                and abs(lvl - level_sent[i]) > LEVEL_MIN_DELTA):
            level_ts[i] = now_mono
            level_sent[i] = lvl
            enqueue_event(LEVEL_EVENT_TYPES[i],
                          {"type": "level", "band": BANDS[i]["name"], "level": round(lvl, 3),
                           "brightness": int(round(255 * (lvl ** FLOW_GAMMA[i]))),
                           "ts": now_wall})

    # "music_playing" is tracked for calibration's quality check ONLY (below,
    # and in run_calibration's music_fraction) - it is NOT used to gate event
    # firing. It used to be: summed raw energy across all 4 bands must clear
    # a flat absolute number (3.0) before ANY band could fire. That was the
    # one non-relative constant left in an otherwise fully self-referential
    # pipeline, and it caused a real bug: testing with an isolated single-band
    # loop (e.g. a kick-only test track) means the other 3 bands sit near
    # zero the whole time, so the 4-band SUM rides on one band's small
    # contribution alone and can drift under the flat threshold - silently
    # disabling ALL bands even though that one band's own ratio/floor/debounce
    # checks are firing correctly. Those three per-band checks already guard
    # against false triggers during genuine silence on their own; this global
    # sum added no protection they didn't already provide, just a failure mode.
    long_avg = np.mean(long_history)
    long_history[long_idx] = float(np.sum(band_energy))
    long_idx = (long_idx + 1) % LONG_HISTORY_LEN
    music_playing = long_avg > 3.0

    with calibration_lock:
        if calibration_state["running"]:
            calibration_state["total_frames"] += 1
            if music_playing:
                calibration_state["music_frames"] += 1
            for i, b in enumerate(BANDS):
                calibration_samples[b["name"]].append(float(ratio[i]))

    # (settings snapshot taken above, before the flow block)
    # A disabled band simply never registers a hit, so it can never start a
    # candidate or fire - no other logic needs to know a band is off.
    # Section-loudness gate: each band's peak decays slowly (~15s
    # half-life), so it stays elevated through a quiet passage that
    # follows a loud one - see SECTION_FLOOR_FRAC for the full rationale.
    # Updated every frame (own-frame-usable, same order as level_peak_ref)
    # regardless of whether a band is enabled, so a re-enabled band's gate
    # isn't stuck on a stale reference.
    section_peak = np.maximum(band_energy, section_peak * SECTION_PEAK_DECAY)
    section_ok = [SECTION_FLOOR_FRAC[i] <= 0.0 or band_energy[i] > SECTION_FLOOR_FRAC[i] * section_peak[i]
                  for i in range(N_BANDS)]

    hits = [band_enabled[i] and (ratio[i] > thresholds[i]) and (band_energy[i] > MIN_BAND_ENERGY[i])
            and section_ok[i] for i in range(N_BANDS)]

    # Low-band click-confirmed rescue (see the CLICK_* constants for the
    # full rationale): a sustain-masked kick whose ratio can't clear the
    # slider still fires if its block carries a proportional broadband
    # click. click flux = per-bin half-wave-rectified rise vs the previous
    # frame - steady content contributes nothing, only fresh broadband
    # attack does. Computed every frame so the previous-frame state is
    # always current, even while no candidate is near.
    click_band = fft_data[CLICK_BINS[0]:CLICK_BINS[1]]
    click_flux = (float(np.sum(np.maximum(0.0, click_band - prev_click_mag)))
                  if prev_click_mag is not None else 0.0)
    prev_click_mag = click_band.copy()
    low_rescued = False
    # thresholds[0] at the slider max is the explicit "mute this band"
    # position - the rescue must stay muted there too.
    if (LOW_CLICK_RESCUE_ENABLED
            and band_enabled[0] and not hits[0]
            and thresholds[0] < THRESHOLD_SLIDER_MAX
            and band_energy[0] > MIN_BAND_ENERGY[0]
            and section_ok[0]):
        low_rise = float(band_energy[0] - short_avg[0])
        if (ratio[0] > max(RESCUE_MIN_RATIO, RESCUE_FRAC * thresholds[0])
                and low_rise > 0.0
                and click_flux > CLICK_FACTOR * low_rise):
            hits[0] = True
            low_rescued = True

    # Expectation-window rescue (see the EXPECT_* constants): a sub-threshold
    # Low rise still fires if it lands within the window around a predicted
    # beat and clears the relaxed bar. A full-threshold hit re-arms the
    # consecutive-rescue budget whether or not the debounce lets it fire.
    low_expected = False
    if hits[0] and not low_rescued:
        expect_consecutive = 0
    elif (EXPECTED_BEAT_RESCUE_ENABLED
            and predict_locked and band_enabled[0] and not hits[0]
            and thresholds[0] < THRESHOLD_SLIDER_MAX
            and expect_consecutive < EXPECT_MAX_CONSECUTIVE
            and abs(now_mono - predict_next_beat) <= EXPECT_WINDOW_MS
            and ratio[0] > max(EXPECT_MIN_RATIO, EXPECT_FRAC * thresholds[0])
            and band_energy[0] > MIN_BAND_ENERGY[0]
            and band_energy[0] > short_avg[0]
            and (SECTION_FLOOR_FRAC[0] <= 0.0
                 or band_energy[0] > SECTION_FLOOR_FRAC[0] * EXPECT_SECTION_RELAX
                    * section_peak[0])):
        hits[0] = True
        low_expected = True

    # Update the short window for the next frame only after using this
    # frame's values, so a frame never gets compared against a window that
    # already includes itself.
    short_history[:, short_idx] = band_energy
    short_idx = (short_idx + 1) % SHORT_HISTORY_LEN

    # Always live - there is no global test-mode gate anymore (the per-band
    # Test buttons in the UI fire single events on demand instead), and no
    # music_playing gate (see its definition above): the per-band
    # ratio/floor/debounce checks already guard against false triggers
    # during real silence.
    strength = float(np.max(np.abs(mono)))

    def _hardness(energy_i, i):
        # 0-1 "how hard is this hit relative to how hard this band has been
        # hitting lately" - the hit's energy over the section peak (the same
        # slow ~15s-memory peak the section gate uses). Gain-invariant, and it
        # differentiates a quiet-section kick from a full-pumping-drop kick
        # (which the raw whole-block `strength` could not - that was peak
        # waveform amplitude across ALL frequencies, so a loud vocal over a
        # soft kick read as "hard"). A uniformly loud passage reads ~1 for
        # every hit; the spread only appears where the band's own level does.
        return round(min(1.0, energy_i / max(float(section_peak[i]), 1e-9)), 3)

    def _fire(i, ratio_i, strength_i, hardness_i, ts_wall_i, tag, expected=False):
        name = BANDS[i]["name"]
        print(f"{name.upper()}{tag}: ratio={ratio_i:.2f} hard={hardness_i:.2f} ts={ts_wall_i:.0f}", flush=True)
        with live_lock:
            # Lets the UI flash the band's meter on every real fire - the
            # client just watches this timestamp for changes.
            live_state["band_last_hit"][i] = ts_wall_i
        if i == 0:
            _predict_on_low_fire(now_mono)
        enqueue_event(BANDS[i]["event_type"],
                      {"type": "beat", "intensity": BANDS[i]["intensity"], "band": name,
                       "strength": strength_i, "hardness": hardness_i,
                       "expected": expected, "ts": ts_wall_i})

    def _start_pending(i):
        pending_active[i] = True
        pending_in_grace[i] = DECAY_RISE_GRACE_FRAMES > 0
        pending_base[i] = float(short_avg[i])
        pending_peak[i] = band_energy[i]
        pending_frames_left[i] = DECAY_RISE_GRACE_FRAMES if DECAY_RISE_GRACE_FRAMES > 0 else DECAY_CONFIRM_FRAMES
        pending_ratio[i] = ratio[i]
        pending_strength[i] = strength
        pending_ts[i] = now_wall

    # Advance candidates already in flight, using this frame's energy, BEFORE
    # considering new candidates this same frame - a band can only have one
    # candidate in flight at a time.
    for i in range(N_BANDS):
        if not pending_active[i]:
            continue

        if pending_in_grace[i]:
            # Grace phase: the peak may still update here (a real hit's
            # attack can take one extra frame to fully develop), but this
            # frame is NEVER also used as a decay-check frame - updating the
            # peak and comparing against it never happen on the same frame
            # (a value compared against itself always looks like "no decay").
            # Fixed length, not open-ended, so a second real hit landing
            # nearby can't get folded into this candidate.
            pending_peak[i] = max(pending_peak[i], band_energy[i])
            pending_frames_left[i] -= 1
            if pending_frames_left[i] <= 0:
                pending_in_grace[i] = False
                pending_frames_left[i] = DECAY_CONFIRM_FRAMES
            continue

        # Decay-check phase: fixed length, never updates the peak.
        pending_frames_left[i] -= 1
        if pending_frames_left[i] > 0:
            continue
        pending_active[i] = False

        # Decay is judged on the rise ABOVE the pre-hit baseline, not on
        # absolute energy. In a busy mix the band's floor is elevated by
        # sustained content underneath the hit (bass line under the kick,
        # wash under the cymbals): a real kick's TOTAL energy may only fall
        # back to 70-90% of its peak because the floor itself is most of
        # that - while the transient part of it (peak minus floor) is long
        # gone. Judging absolute energy against the peak rejected exactly
        # those kicks (the "energy stayed at 65-88% of peak" log lines in
        # busy sections). Judging the rise instead adapts to context: in a
        # quiet mix the baseline is ~0 and this reduces to the old check;
        # in a busy mix it isolates the transient from the floor it rode
        # in on.
        rise_now = max(0.0, band_energy[i] - pending_base[i])
        rise_peak = max(1e-9, pending_peak[i] - pending_base[i])
        if rise_now >= DECAY_FACTOR * rise_peak:
            # Still elevated above the pre-hit baseline: sustained content
            # (a bass note/riff, a held cymbal wash, a sung vowel), not a
            # percussive strike. Reject - and roll this band's lockout back
            # so the rejected candidate doesn't eat a REAL hit landing right
            # after it. Without the rollback, sustained content would keep
            # the band cycling through candidate windows + leftover lockout,
            # and real hits landing in between would silently vanish.
            last_band_fire[i] = now_mono - band_lockout_ms[i] - 1.0
            print(f"{BANDS[i]['name'].upper()} (rejected: transient still at "
                  f"{rise_now/rise_peak*100:.0f}% of its rise above baseline, "
                  f"needed under {DECAY_FACTOR*100:.0f}%)", flush=True)
            continue
        # Confirmed hits fire frames after the peak, on the decay - use the
        # candidate's stored PEAK energy for hardness, not the (already-fallen)
        # current-frame energy.
        _fire(i, pending_ratio[i], pending_strength[i], _hardness(pending_peak[i], i),
              pending_ts[i], " (confirmed)")

    # Start new candidates. Every band is fully independent - each has its
    # own dedicated light and event type, so nothing here suppresses or
    # prioritizes anything: no kick-priority ordering, no winner-take-all,
    # and no post-kick suppression window. That window was a relic of the
    # single-shared-light era, and it was quietly the biggest source of
    # "the other lights feel dead": with kicks landing every ~470ms in
    # typical 4/4 music, a 500ms mute meant snare/hats/cymbals were
    # suppressed essentially ALL the time whenever kick was firing well -
    # they only got to flash when kick FAILED, which is exactly the
    # trading-off behavior that made the show feel inconsistent.
    # Cross-band spectral bleed (a kick's harmonics reaching into the snare
    # band) is handled where it belongs instead: per-band thresholds tuned
    # with the live meters, plus envelope confirmation on every band.
    for i in range(N_BANDS):
        if not (hits[i] and not pending_active[i]
                and (now_mono - last_band_fire[i] > band_lockout_ms[i])):
            continue
        last_band_fire[i] = now_mono
        if DECAY_CONFIRM_ENABLED[i]:
            _start_pending(i)
        else:
            # Instant fire (see DECAY_CONFIRM_ENABLED): no envelope check,
            # and no ~128ms confirmation latency either. The (rescued)/
            # (expected) tags mark sub-threshold hits admitted by the click
            # confirmation / expectation window, so field logs can validate
            # those paths against real music.
            is_expected = (i == 0 and low_expected)
            if is_expected:
                expect_consecutive += 1
            tag = (" (rescued)" if (i == 0 and low_rescued)
                   else " (expected)" if is_expected else "")
            _fire(i, ratio[i], strength, _hardness(band_energy[i], i), now_wall,
                  tag, expected=is_expected)

    # Tempo-tracker lock maintenance. Runs every frame; only does work while
    # locked. No event emission here - this purely keeps predict_locked and
    # predict_next_beat current for the expectation-window rescue above to
    # consult (see the "Internal tempo tracker" comment near PREDICT_HISTORY).
    if predict_locked:
        if now_mono - predict_onsets[-1] > PREDICT_LOSE_LOCK_BEATS * predict_interval:
            # Real beats stopped coming (breakdown / song end / tempo jump):
            # drop the lock. Clear the onset history too - without this, the
            # FIRST kick after a long silence instantly "relocks" on the
            # STALE pre-silence intervals still sitting in the deque and
            # predicts at the old tempo (sim-caught bug). After a real lock
            # loss, a fresh lock must be earned from ~6 new beats.
            predict_locked = False
            predict_onsets.clear()
            print("PREDICT: lock lost (no real beats)", flush=True)
        elif now_mono > predict_next_beat + 0.5 * predict_interval:
            # The expected beat came and went with no real fire to
            # re-anchor us (masked kick, dropped block) - coast one
            # interval forward and keep going until lose-lock trips.
            predict_next_beat += predict_interval

# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
def run_calibration():
    with calibration_lock:
        for b in BANDS:
            calibration_samples[b["name"]] = []
        calibration_state["running"] = True
        calibration_state["result"] = None
        calibration_state["music_frames"] = 0
        calibration_state["total_frames"] = 0

    time.sleep(CALIBRATE_DURATION_S)

    with calibration_lock:
        calibration_state["running"] = False
        band_stats = {}
        suggested_band_thresholds = []
        n_samples = 0
        for b in BANDS:
            arr = np.array(calibration_samples[b["name"]]) if calibration_samples[b["name"]] else np.array([1.0])
            n_samples = max(n_samples, len(arr))
            # 90th percentile of the ratio signal: separates "typical
            # jitter around baseline" from "the loud transient moments"
            # reasonably well for a short listening window. Capped BELOW the
            # slider max: the ratio itself is capped at RATIO_CAP (== slider
            # max) and the hit check is a strict ">", so a threshold sitting
            # exactly at the max can never fire - that position is the
            # explicit "mute this band" setting (labeled in the UI). A
            # calibration suggestion must never land there, or Apply & Save
            # would silently mute a spiky band.
            suggested = round(min(float(np.percentile(arr, 90)), THRESHOLD_SLIDER_MAX - 0.5), 2)
            suggested_band_thresholds.append(suggested)
            band_stats[b["name"]] = {
                "suggested_threshold": suggested,
                "median": round(float(np.median(arr)), 2),
                "max": round(float(np.max(arr)), 2),
            }
        music_fraction = calibration_state["music_frames"] / max(1, calibration_state["total_frames"])
        result = {
            "bands": band_stats,
            "suggested_band_thresholds": suggested_band_thresholds,
            "samples": n_samples,
            "music_fraction": round(music_fraction, 2),
        }
        if music_fraction < 0.5:
            # Suggestions from a mostly-silent window are just ratio jitter
            # around 1.0 - applying them would make every band hair-trigger.
            result["warning"] = ("Little or no music was detected while listening. "
                                 "Play music at normal volume and calibrate again "
                                 "before applying these.")
        calibration_state["result"] = result
    print(f"Calibration complete: {result}", flush=True)

# ---------------------------------------------------------------------------
# Persist settings via Supervisor API (source of truth for addon options)
# ---------------------------------------------------------------------------
def save_options_to_supervisor(new_options):
    try:
        r = requests.post(SUPERVISOR_OPTIONS_URL, headers=HEADERS, json={"options": new_options}, timeout=5)
        return r.ok, r.text
    except Exception as e:
        return False, str(e)

# ---------------------------------------------------------------------------
# Ingress web UI
# ---------------------------------------------------------------------------
# NOTE: all fetch() calls in the page JS use RELATIVE paths (no leading "/").
# Home Assistant's ingress proxy serves this page behind a per-session path
# prefix - an absolute "/status" would bypass that prefix and 404. Relative
# paths resolve correctly against whatever prefix the browser is already on.
BAND_LABELS = ["Low", "Mid", "High"]

PAGE_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<title>Trancistor</title>
<style>
  body { font-family: -apple-system, Arial, sans-serif; background:#111; color:#eee; margin:0; padding:16px; }
  h2 { margin-top:0; }
  .card { background:#1b1b1b; border-radius:10px; padding:14px 16px; margin-bottom:14px; }
  .meter-label { display:flex; justify-content:space-between; font-size:13px; margin-bottom:4px; }
  .meter { height:14px; background:#222; border-radius:8px; overflow:hidden; position:relative;
           transition: box-shadow 0.25s; }
  .meter .fill { height:100%; width:0%; background:linear-gradient(90deg,#4ade80,#facc15,#ef4444);
                 transition: width 0.1s linear; }
  .meter .threshold-line { position:absolute; top:0; bottom:0; width:2px; background:#fff;
                           transition: left 0.1s linear; }
  .band-block.hit .meter { box-shadow: 0 0 0 2px #4ade80; }
  #spec { width:100%; height:170px; display:block; background:#161616; border-radius:8px; }
  details.flow { margin-top:10px; border:1px solid #2a2a2a; border-radius:8px; padding:6px 10px; }
  details.flow summary { cursor:pointer; font-size:13px; color:#8ab4f8; user-select:none; }
  details.flow[open] summary { margin-bottom:4px; }
  .band-block { margin-bottom:16px; }
  .band-block:last-child { margin-bottom:0; }
  .band-block.disabled .meter, .band-block.disabled input[type=range] { opacity:0.35; }
  .band-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; }
  .band-header .name { font-weight:600; }
  .toggle-label { display:flex; align-items:center; gap:6px; font-size:13px; color:#ccc; }
  .toggle-label input { width:auto; }
  label { display:block; font-size:13px; margin:10px 0 4px; color:#ccc; }
  input, select, textarea { width:100%; box-sizing:border-box; padding:8px; border-radius:6px; border:1px solid #333; background:#222; color:#eee; font-size:14px; }
  input[type=range] { padding:0; }
  button { padding:10px 16px; border-radius:8px; border:none; font-size:14px; font-weight:600; cursor:pointer; margin-top:10px; margin-right:8px; }
  .btn-primary { background:#4ade80; color:#111; }
  .btn-secondary { background:#333; color:#eee; }
  .row { display:flex; gap:12px; align-items:center; }
  .row > div { flex:1; }
  .small { color:#999; font-size:12px; margin-top:6px; }
  .suggested { background:#222b22; border:1px solid #4ade80; border-radius:8px; padding:10px; margin-top:10px; }
  table { width:100%; border-collapse: collapse; font-size:13px; margin-top:8px; }
  td, th { text-align:left; padding:4px 6px; border-bottom:1px solid #333; }
</style>
</head>
<body>
  <h2>Trancistor</h2>

  <div class="card">
    <div class="band-header" style="margin-bottom:4px">
      <span class="name">Spectrum</span>
      <span class="small" style="margin:0">what the mic hears</span>
    </div>
    <div class="small" style="margin-top:0">Raw energy by frequency (log scale, like hearing).
      The shaded regions are your three bands - content outside every shaded region can't
      trigger anything. Use this view to place the band edges on real music.</div>
    <canvas id="spec" style="margin-top:8px"></canvas>
  </div>

  <div class="card">
    <div class="band-header" style="margin-bottom:4px">
      <span class="name">Detector</span>
      <span class="small" style="margin:0">what the detector reacts to</span>
    </div>
    <div class="small" style="margin-top:0">Each bar is that band's "how much louder than a moment
      ago" ratio, hanging near its recent peak. Each bar auto-fits its own scale to what that band
      actually produces (the "bar full = N" label). Drag the sensitivity slider until the white
      marker sits just above where real hits peak - the meter outline flashes green each time the
      band fires. Debounce is the minimum time between two hits regardless of loudness.</div>
    <div id="meters" style="margin-top:10px"></div>
  </div>

  <div class="card">
    <button class="btn-secondary" onclick="saveSettings()">Save Settings</button>
    <div class="small" id="save-status"></div>
  </div>

  <div class="card">
    <button class="btn-primary" onclick="calibrate()">Calibrate Now (6s)</button>
    <div class="small">Play music at normal volume, then click this. It sets a starting-point
      slider position per band - fine-tune by hand afterward using the live meters above.</div>
    <div class="small" id="cal-status"></div>
    <div id="suggested" class="suggested" style="display:none">
      <table id="suggested-table"></table>
      <button class="btn-primary" onclick="applySuggested()">Apply &amp; Save</button>
    </div>
  </div>

  <div class="card">
    <div class="band-header" style="margin-bottom:4px">
      <span class="name">Protect your Home Assistant storage</span>
    </div>
    <div class="small" style="margin-top:0">This addon's flashing and flowing lights change many
      times a second while music plays. If Home Assistant logs every one of those changes to its
      history database, that's a lot of unnecessary writes piling up over time - which can wear out
      storage (especially SD cards) for a graph nobody needs (a strobing light's history isn't
      useful to look at). Add your lights below to stop Home Assistant recording their frequent
      changes - groups (a Zigbee2MQTT or Hue group) work exactly like individual lights, just pick
      the group itself.</div>
    <input id="light-search" type="text" placeholder="Search your lights..." style="margin-top:8px" oninput="renderLightList()">
    <div id="light-list" style="max-height:220px; overflow-y:auto; margin-top:8px; border:1px solid #333; border-radius:6px; padding:8px 10px"></div>
    <div class="small" id="light-list-status"></div>
    <div id="recorder-yaml-block" style="display:none; margin-top:10px">
      <label style="margin-top:0">Add this to <code>configuration.yaml</code>, then restart Home Assistant Core:</label>
      <textarea id="recorder-yaml" readonly rows="12" style="font-family:monospace; font-size:12px; resize:vertical"></textarea>
      <button class="btn-secondary" onclick="copyRecorderYaml()">Copy to clipboard</button>
      <span class="small" id="copy-status"></span>
    </div>
  </div>

<script>
const BAND_LABELS = ["Low", "Mid", "High"];
const SLIDER_MIN = 1.1;
const SLIDER_MAX = 20;
let lastSuggested = null;
let bandThresholds = [0, 0, 0];
let bandLockouts = [320, 320, 320];
let bandEnabled = [true, true, true];
let bandHz = [25, 80, 100, 800, 8000, 16000];
let flowAttack = [0.5, 0.5, 0.5];
let flowRelease = [0.2, 0.2, 0.2];
let flowGamma = [2.2, 2.2, 2.2];
let flowMemory = [30, 30, 30];

// Per-band meter scale: rides the highest ratio the band has actually
// produced (with slow decay), so low/mid - whose ratios live around 1.5-5 -
// use the full bar instead of the bottom sliver of a shared 1.1-20 scale.
// Floors: never below 2.0, never below the threshold itself (so the white
// marker always stays on the bar).
let scaleMax = [4, 4, 4];
let lastHit = [0, 0, 0];

function buildMeters(){
  const el = document.getElementById('meters');
  el.innerHTML = BAND_LABELS.map((label, i) => `
    <div class="band-block" id="band-block-${i}">
      <div class="band-header">
        <span class="name">${label}</span>
        <label class="toggle-label"><input id="band-enabled-${i}" type="checkbox" checked oninput="onEnabledInput(${i})">Enabled</label>
      </div>
      <div class="meter-label"><span id="scale-max-${i}" style="color:#777"></span><span id="band-val-${i}">0.00</span></div>
      <div class="meter"><div id="band-fill-${i}" class="fill"></div><div id="band-thresh-${i}" class="threshold-line"></div></div>
      <label style="margin-top:8px">Sensitivity <span id="thresh-val-${i}"></span></label>
      <input id="band-slider-${i}" type="range" min="${SLIDER_MIN}" max="${SLIDER_MAX}" step="0.1" oninput="onSliderInput(${i})">
      <div class="small" style="display:flex;justify-content:space-between;margin-top:0">
        <span>&larr; more sensitive (triggers easily)</span><span>more selective &rarr; (max = mute band)</span>
      </div>
      <label style="margin-top:8px">Debounce <span id="lockout-val-${i}"></span>ms</label>
      <input id="lockout-slider-${i}" type="range" min="20" max="3000" step="10" oninput="onLockoutInput(${i})">
      <div class="small">Minimum time between two hits on this band, regardless of loudness.</div>
      <div class="row" style="margin-top:8px">
        <div><label style="margin-top:0">Range low (Hz)</label>
          <input id="hz-lo-${i}" type="number" min="20" max="20000" step="5" oninput="onHzInput(${i})"></div>
        <div><label style="margin-top:0">Range high (Hz)</label>
          <input id="hz-hi-${i}" type="number" min="20" max="20000" step="5" oninput="onHzInput(${i})"></div>
      </div>
      <div class="small" id="hz-eff-${i}" style="color:#8ab4f8"></div>
      <div class="small">Frequency range this band listens to. Bands may overlap or leave gaps
        (a gap means nothing reacts to those frequencies). Applied on Save Settings.</div>
      <details class="flow">
        <summary>Flow (breathing light) tuning</summary>
        <label style="margin-top:6px">Rise speed <span id="flow-attack-val-${i}"></span></label>
        <input id="flow-attack-${i}" type="range" min="0.05" max="1" step="0.05" oninput="onFlowInput(${i})">
        <div class="small">How fast the flow light jumps when this band gets louder.
          High = snappy/punchy, low = dreamy late bloom.</div>
        <label>Fall speed <span id="flow-release-val-${i}"></span></label>
        <input id="flow-release-${i}" type="range" min="0.02" max="0.6" step="0.02" oninput="onFlowInput(${i})">
        <div class="small">How fast it breathes back down. Low = long glowing tails,
          high = tight pulsing that follows every dip.</div>
        <label>Contrast <span id="flow-gamma-val-${i}"></span></label>
        <input id="flow-gamma-${i}" type="range" min="0.8" max="4" step="0.1" oninput="onFlowInput(${i})">
        <div class="small">High = stays near-dark and blooms on peaks (dramatic).
          Low = hovers around mid-brightness (gentle wash).</div>
        <label>Loudness memory <span id="flow-memory-val-${i}"></span>s</label>
        <input id="flow-memory-${i}" type="range" min="5" max="90" step="1" oninput="onFlowInput(${i})">
        <div class="small">How long "loud" is remembered: quiet passages look dim for about
          this long before re-brightening. Subtle - judge changes over a full song.</div>
      </details>
      <button class="btn-secondary" onclick="testBand(${i})">Test flash</button>
    </div>
  `).join('');
}
buildMeters();

function onSliderInput(i){
  bandThresholds[i] = parseFloat(document.getElementById(`band-slider-${i}`).value);
  updateThreshReadout(i);
}

function updateThreshReadout(i){
  document.getElementById(`thresh-val-${i}`).textContent = bandThresholds[i].toFixed(1);
}

function onLockoutInput(i){
  bandLockouts[i] = parseFloat(document.getElementById(`lockout-slider-${i}`).value);
  document.getElementById(`lockout-val-${i}`).textContent = bandLockouts[i].toFixed(0);
}

function onHzInput(i){
  const lo = parseFloat(document.getElementById(`hz-lo-${i}`).value);
  const hi = parseFloat(document.getElementById(`hz-hi-${i}`).value);
  if (!isNaN(lo)) bandHz[2*i] = lo;
  if (!isNaN(hi)) bandHz[2*i+1] = hi;
  updateHzEffective(i);
}

// The analysis has ~23.4Hz resolution and edges snap to the nearest bin
// (upper edge exclusive), so the numbers you type are NOT exactly what the
// detector hears. This line shows the truth, and warns before Save if a
// range is too narrow to cover even one bin (the server rejects those).
const BIN_HZ = 23.4375;
function updateHzEffective(i){
  const el = document.getElementById(`hz-eff-${i}`);
  const lb = Math.max(0, Math.round(bandHz[2*i] / BIN_HZ));
  const hb = Math.max(0, Math.round(bandHz[2*i+1] / BIN_HZ));
  if (hb <= lb) {
    el.textContent = `TOO NARROW - needs at least one ~${BIN_HZ.toFixed(0)} Hz bin; Save will reject this`;
    el.style.color = '#f87171';
    return;
  }
  const fLo = Math.max(0, (lb - 0.5) * BIN_HZ).toFixed(0);
  const fHi = ((hb - 0.5) * BIN_HZ).toFixed(0);
  el.textContent = `actually hears: ~${fLo}-${fHi} Hz (FFT bins ${lb}-${hb - 1})`;
  el.style.color = '#8ab4f8';
}

function onEnabledInput(i){
  bandEnabled[i] = document.getElementById(`band-enabled-${i}`).checked;
  document.getElementById(`band-block-${i}`).classList.toggle('disabled', !bandEnabled[i]);
}

function onFlowInput(i){
  flowAttack[i] = parseFloat(document.getElementById(`flow-attack-${i}`).value);
  flowRelease[i] = parseFloat(document.getElementById(`flow-release-${i}`).value);
  flowGamma[i] = parseFloat(document.getElementById(`flow-gamma-${i}`).value);
  flowMemory[i] = parseFloat(document.getElementById(`flow-memory-${i}`).value);
  updateFlowReadouts(i);
}

function updateFlowReadouts(i){
  document.getElementById(`flow-attack-val-${i}`).textContent = flowAttack[i].toFixed(2);
  document.getElementById(`flow-release-val-${i}`).textContent = flowRelease[i].toFixed(2);
  document.getElementById(`flow-gamma-val-${i}`).textContent = flowGamma[i].toFixed(1);
  document.getElementById(`flow-memory-val-${i}`).textContent = flowMemory[i].toFixed(0);
}

function setFlowSliders(i){
  document.getElementById(`flow-attack-${i}`).value = flowAttack[i];
  document.getElementById(`flow-release-${i}`).value = flowRelease[i];
  document.getElementById(`flow-gamma-${i}`).value = flowGamma[i];
  document.getElementById(`flow-memory-${i}`).value = flowMemory[i];
  updateFlowReadouts(i);
}

async function testBand(i){
  await fetch('test', {method:'POST', headers:{'Content-Type':'application/json'},
                       body: JSON.stringify({band: BAND_LABELS[i].toLowerCase()})});
}

async function fetchStatus(){
  try{
    const r = await fetch('status');
    const j = await r.json();
    if (j.spectrum) specTarget = j.spectrum;
    for (let i = 0; i < BAND_LABELS.length; i++) {
      const peak = j.band_peak[i];
      const thresh = j.effective_thresholds[i];
      document.getElementById(`band-val-${i}`).textContent = peak.toFixed(2);
      // Auto-fitting per-band scale (see scaleMax above). Bar and threshold
      // marker share this scale, so "marker just above where hits peak"
      // stays a truthful visual - the slider's own 1.1-20 travel is just an
      // input knob, its numeric readout is what you record.
      scaleMax[i] = Math.max(2.0, thresh * 1.1, peak * 1.05, scaleMax[i] * 0.998);
      const toPct = (v) => Math.max(0, Math.min(100, (v - 1) / (scaleMax[i] - 1) * 100));
      document.getElementById(`band-fill-${i}`).style.width = toPct(peak) + '%';
      document.getElementById(`band-thresh-${i}`).style.left = toPct(thresh) + '%';
      document.getElementById(`scale-max-${i}`).textContent = 'bar full = ' + scaleMax[i].toFixed(1);
      if (j.band_last_hit && j.band_last_hit[i] !== lastHit[i]) {
        lastHit[i] = j.band_last_hit[i];
        if (window._loaded) {
          const blk = document.getElementById(`band-block-${i}`);
          blk.classList.add('hit');
          setTimeout(() => blk.classList.remove('hit'), 200);
        }
      }
    }

    if (!window._loaded) {
      for (let i = 0; i < BAND_LABELS.length; i++) {
        bandThresholds[i] = j.effective_thresholds[i];
        document.getElementById(`band-slider-${i}`).value = bandThresholds[i];
        updateThreshReadout(i);
        bandLockouts[i] = j.settings.band_lockout_ms[i];
        document.getElementById(`lockout-slider-${i}`).value = bandLockouts[i];
        document.getElementById(`lockout-val-${i}`).textContent = bandLockouts[i].toFixed(0);
        bandEnabled[i] = j.settings.band_enabled[i];
        document.getElementById(`band-enabled-${i}`).checked = bandEnabled[i];
        document.getElementById(`band-block-${i}`).classList.toggle('disabled', !bandEnabled[i]);
        if (j.settings.band_hz) {
          bandHz[2*i] = j.settings.band_hz[2*i];
          bandHz[2*i+1] = j.settings.band_hz[2*i+1];
          document.getElementById(`hz-lo-${i}`).value = bandHz[2*i];
          document.getElementById(`hz-hi-${i}`).value = bandHz[2*i+1];
          updateHzEffective(i);
        }
        if (j.settings.flow_attack) {
          flowAttack[i] = j.settings.flow_attack[i];
          flowRelease[i] = j.settings.flow_release[i];
          flowGamma[i] = j.settings.flow_gamma[i];
          flowMemory[i] = j.settings.flow_memory_s[i];
          setFlowSliders(i);
        }
      }
      window._loaded = true;
    }
  }catch(e){}
}

async function calibrate(){
  document.getElementById('cal-status').textContent = 'Listening for 6 seconds - play music now...';
  document.getElementById('suggested').style.display = 'none';
  const r = await fetch('calibrate', {method: 'POST'});
  const j = await r.json();
  lastSuggested = j;
  document.getElementById('cal-status').textContent =
    `Done (${j.samples} samples).` + (j.warning ? ' WARNING: ' + j.warning : '');
  const rows = BAND_LABELS.map((label, i) => {
    const name = Object.keys(j.bands)[i];
    const b = j.bands[name];
    return `<tr><td>${label}</td><td>suggested ${b.suggested_threshold}</td><td>median ${b.median}</td><td>max ${b.max}</td></tr>`;
  }).join('');
  document.getElementById('suggested-table').innerHTML = `<tr><th>Band</th><th>Suggested</th><th>Median</th><th>Max</th></tr>${rows}`;
  document.getElementById('suggested').style.display = 'block';
}

async function applySuggested(){
  if (!lastSuggested) return;
  for (let i = 0; i < BAND_LABELS.length; i++) {
    bandThresholds[i] = lastSuggested.suggested_band_thresholds[i];
    document.getElementById(`band-slider-${i}`).value = bandThresholds[i];
    updateThreshReadout(i);
  }
  await saveSettings();
}

async function saveSettings(){
  const payload = {
    band_thresholds: bandThresholds,
    band_lockout_ms: bandLockouts,
    band_enabled: bandEnabled,
    band_hz: bandHz,
    flow_attack: flowAttack,
    flow_release: flowRelease,
    flow_gamma: flowGamma,
    flow_memory_s: flowMemory,
  };
  const st = document.getElementById('save-status');
  st.textContent = 'Saving...';
  st.style.color = '';
  const r = await fetch('save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  const j = await r.json();
  if (!j.ok) {
    st.textContent = 'Save failed: ' + j.error;
    st.style.color = '#f87171';
  } else if (j.warnings && j.warnings.length) {
    // Something was rejected and fell back - say so loudly, and re-sync all
    // controls from the server below so the page shows what was ACTUALLY
    // saved, never the rejected input.
    st.textContent = 'Saved with changes: ' + j.warnings.join(' | ');
    st.style.color = '#fbbf24';
  } else {
    st.textContent = 'Saved.';
  }
  window._loaded = false;  // next status poll re-syncs every control from the server
}

// ---------------------------------------------------------------------------
// Spectrum visualizer: 96 log-spaced bars (20Hz-20kHz) from the backend,
// drawn at display refresh rate with exponential smoothing between the
// ~10/s data polls (fast attack, slower release - classic analyzer feel).
// All the animation cost lives here in the browser; the Pi only serializes
// the same FFT it already computes.
// ---------------------------------------------------------------------------
const SPEC_N = 96, SPEC_FMIN = 20, SPEC_FMAX = 20000, SPEC_DB_RANGE = 50;
const BAND_FILL_COLORS = ['rgba(96,165,250,0.20)', 'rgba(74,222,128,0.16)', 'rgba(244,114,182,0.16)'];
const BAND_TEXT_COLORS = ['#60a5fa', '#4ade80', '#f472b6'];
const SPEC_GRID = [[31,'31'],[63,'63'],[125,'125'],[250,'250'],[500,'500'],
                   [1000,'1k'],[2000,'2k'],[4000,'4k'],[8000,'8k'],[16000,'16k']];
let specTarget = new Array(SPEC_N).fill(0);
let specShow = new Array(SPEC_N).fill(-120);   // smoothed, in dB
let specMaxDb = -40;                           // slow auto-ranging ceiling

function xOfHz(hz, W){
  return W * Math.log(hz / SPEC_FMIN) / Math.log(SPEC_FMAX / SPEC_FMIN);
}

function drawSpec(){
  const cv = document.getElementById('spec');
  const W = cv.clientWidth, H = cv.clientHeight;
  if (W > 0) {
    const dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(W * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    // Band regions (behind everything) - live from the Hz inputs, so edits
    // are visible here even before you hit Save.
    for (let b = 0; b < BAND_LABELS.length; b++) {
      const lo = Math.max(SPEC_FMIN, bandHz[2*b]), hi = Math.min(SPEC_FMAX, bandHz[2*b+1]);
      if (hi <= lo) continue;
      const x1 = xOfHz(lo, W), x2 = xOfHz(hi, W);
      ctx.fillStyle = BAND_FILL_COLORS[b];
      ctx.fillRect(x1, 0, x2 - x1, H - 14);
      ctx.fillStyle = BAND_TEXT_COLORS[b];
      ctx.font = '11px sans-serif';
      ctx.fillText(BAND_LABELS[b], x1 + 4, 13);
    }

    // Frequency gridlines + Hz labels at octaves.
    ctx.strokeStyle = '#2c2c2c';
    ctx.fillStyle = '#888';
    ctx.font = '10px sans-serif';
    for (const [f, lab] of SPEC_GRID) {
      const x = xOfHz(f, W);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H - 14); ctx.stroke();
      ctx.fillText(lab, x - ctx.measureText(lab).width / 2, H - 3);
    }

    // Bars: dB scale inside a sliding auto-ranged window, so quiet and loud
    // tracks both fill the display sensibly.
    let frameMax = -120;
    for (let i = 0; i < SPEC_N; i++) {
      const db = 20 * Math.log10(Math.max(specTarget[i], 1e-4));
      if (db > frameMax) frameMax = db;
      specShow[i] += (db - specShow[i]) * (db > specShow[i] ? 0.5 : 0.12);
    }
    specMaxDb = Math.max(frameMax, specMaxDb - 0.05, -40);
    const barW = W / SPEC_N, floorDb = specMaxDb - SPEC_DB_RANGE;
    ctx.fillStyle = '#9ca3af';
    for (let i = 0; i < SPEC_N; i++) {
      const h = Math.max(0, Math.min(1, (specShow[i] - floorDb) / SPEC_DB_RANGE)) * (H - 18);
      ctx.fillRect(i * barW + 0.5, H - 14 - h, Math.max(1, barW - 1), h);
    }
  }
  requestAnimationFrame(drawSpec);
}
requestAnimationFrame(drawSpec);

setInterval(fetchStatus, 100);
fetchStatus();

// ---------------------------------------------------------------------------
// Recorder-protection light picker. Fetches the user's real light entities
// (incl. groups - a Zigbee2MQTT/Hue group is just another light.* entity,
// no special-casing needed) once on load, so nobody has to know or copy an
// entity_id by hand. Purely a YAML *generator* - it never touches Home
// Assistant's config itself; the user still pastes the result in and
// restarts Core themselves.
// ---------------------------------------------------------------------------
let allLights = [];
let selectedLights = new Set();

async function fetchLights(){
  const status = document.getElementById('light-list-status');
  try {
    const r = await fetch('lights');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    allLights = await r.json();
    status.textContent = allLights.length
      ? ''
      : 'No light entities found in Home Assistant.';
    renderLightList();
  } catch (e) {
    status.textContent = 'Could not load your lights (' + e.message + ').';
  }
}

function renderLightList(){
  const filter = document.getElementById('light-search').value.trim().toLowerCase();
  const el = document.getElementById('light-list');
  const shown = allLights.filter(l =>
    !filter || l.friendly_name.toLowerCase().includes(filter) || l.entity_id.toLowerCase().includes(filter));
  el.innerHTML = shown.map(l => `
    <label class="toggle-label" style="margin:4px 0">
      <input type="checkbox" data-entity="${l.entity_id}"
             ${selectedLights.has(l.entity_id) ? 'checked' : ''}
             onchange="onLightToggle('${l.entity_id}', this.checked)">
      ${l.friendly_name} <span style="color:#777">(${l.entity_id})</span>
    </label>
  `).join('') || '<div class="small">No lights match your search.</div>';
}

function onLightToggle(entityId, checked){
  if (checked) selectedLights.add(entityId); else selectedLights.delete(entityId);
  updateRecorderYaml();
}

function updateRecorderYaml(){
  const block = document.getElementById('recorder-yaml-block');
  const ids = Array.from(selectedLights).sort();
  if (!ids.length) { block.style.display = 'none'; return; }
  // NOTE: '\\n' (double backslash) is deliberate, not a typo. PAGE_HTML is a
  // normal (non-raw) Python triple-quoted string, so Python's own escape
  // processing collapses a single '\\n' into a real newline BEFORE this ever
  // reaches the browser - landing a raw line break inside a single-quoted JS
  // string, which is a syntax error there (silently breaks the ENTIRE page
  // script with no console output beyond "Invalid or unexpected token").
  // Escaping the backslash here is what makes Python hand the browser the
  // literal 2-character sequence backslash+n, for the JS engine to interpret.
  const list = ids.map(id => `      - ${id}`).join('\\n');
  document.getElementById('recorder-yaml').value =
`recorder:
  exclude:
    entities:
${list}

logbook:
  exclude:
    entities:
${list}`;
  block.style.display = 'block';
}

async function copyRecorderYaml(){
  const ta = document.getElementById('recorder-yaml');
  const status = document.getElementById('copy-status');
  ta.select();
  ta.setSelectionRange(0, ta.value.length);
  // The modern Clipboard API is blocked inside Home Assistant's ingress
  // iframe (it needs a permissions-policy grant from the PARENT page, which
  // we have no control over as the embedded addon). Try it anyway in case
  // some setup allows it, then fall back to the older execCommand('copy') -
  // it works differently (copies the current on-page selection rather than
  // needing its own permission grant) and reliably works in this iframe
  // context where the modern API doesn't. If even that fails, the text is
  // still left selected so the user can just press Ctrl/Cmd+C themselves.
  try {
    await navigator.clipboard.writeText(ta.value);
    status.textContent = 'Copied!';
  } catch (e) {
    try {
      if (document.execCommand('copy')) {
        status.textContent = 'Copied!';
      } else {
        status.textContent = 'Press Ctrl/Cmd+C to copy the selected text.';
      }
    } catch (e2) {
      status.textContent = 'Press Ctrl/Cmd+C to copy the selected text.';
    }
  }
  setTimeout(() => { status.textContent = ''; }, 4000);
}

fetchLights();
</script>
</body>
</html>
"""

class IngressHandler(http.server.BaseHTTPRequestHandler):
    # Only the HA ingress proxy should ever reach this server: config.json
    # maps no host port, so the LAN can't see it, but other addon containers
    # share the internal docker network and could otherwise POST /save.
    # 172.30.32.2 is the Supervisor's ingress gateway address - the same
    # source HA's own ingress examples allow.
    INGRESS_GATEWAY_IP = '172.30.32.2'

    # HTTP/1.1 for keep-alive: the UI polls /status 10x per second, and the
    # BaseHTTPRequestHandler default (HTTP/1.0) opens a fresh TCP connection
    # through the ingress proxy for every poll. Safe here because _send()
    # always sets Content-Length (1.1 requires it to frame responses).
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass  # quiet - avoid spamming the addon log with every HTTP request

    def _authorized(self):
        ok = self.client_address[0] == self.INGRESS_GATEWAY_IP
        if not ok:
            # Deliberately loud: if an HAOS update ever changes the gateway
            # address, this line is what explains the suddenly-403ing UI.
            print(f"Rejected request from {self.client_address[0]} (not the ingress gateway)", flush=True)
        return ok

    def _route(self):
        # Exact path with any query string stripped - endswith() matching
        # would also route /anythingstatus to /status.
        return self.path.split('?', 1)[0].rstrip('/')

    def do_GET(self):
        if not self._authorized():
            self._send(403, b'{}', 'application/json')
            return
        path = self._route()
        if path in ('', '/trancistor'):
            self._send(200, PAGE_HTML.encode('utf-8'), 'text/html')
        elif path == '/status':
            with live_lock:
                live = dict(live_state)
            with settings_lock:
                s = dict(settings)
            self._send(200, json.dumps({
                **live,
                "settings": s,
                "effective_thresholds": effective_thresholds(s),
            }).encode('utf-8'), 'application/json')
        elif path == '/lights':
            # Read-only lookup of the user's actual light entities (incl.
            # groups - a Zigbee2MQTT/Hue group is just a regular light.*
            # entity, indistinguishable here from a single bulb), so the
            # Recorder-protection picker below never requires typing or
            # copying an entity_id by hand. Uses the same HA API access the
            # addon already has for firing events - no extra permission.
            try:
                r = requests.get(HA_STATES_URL, headers=HEADERS, timeout=5)
                r.raise_for_status()
                lights = sorted(
                    ({"entity_id": s["entity_id"],
                      "friendly_name": s.get("attributes", {}).get("friendly_name", s["entity_id"])}
                     for s in r.json() if s["entity_id"].startswith("light.")),
                    key=lambda x: x["friendly_name"].lower())
                self._send(200, json.dumps(lights).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(502, json.dumps({"error": str(e)}).encode('utf-8'), 'application/json')
        else:
            self._send(404, b'{}', 'application/json')

    def do_POST(self):
        if not self._authorized():
            self._send(403, b'{}', 'application/json')
            return
        path = self._route()
        if path == '/calibrate':
            # Check-and-claim under the lock, not a bare "if not running" -
            # otherwise two near-simultaneous requests (double-click, a
            # second browser tab) can both see "not running" before either
            # sets it, and both spawn a run_calibration thread that resets
            # and writes the same calibration_samples dict concurrently.
            with calibration_lock:
                should_start = not calibration_state["running"]
                if should_start:
                    calibration_state["running"] = True
            if should_start:
                threading.Thread(target=run_calibration, daemon=True).start()
            # Wait for the run to actually finish - whether it was just
            # started above, or was already in flight from a concurrent
            # request (double-click, second browser tab) - so a second
            # request can never return stale/empty data from a run that
            # hasn't completed yet.
            deadline = time.monotonic() + CALIBRATE_DURATION_S + 2.0
            while calibration_state["running"] and time.monotonic() < deadline:
                time.sleep(0.1)
            with calibration_lock:
                result = calibration_state["result"] or {}
            self._send(200, json.dumps(result).encode('utf-8'), 'application/json')
        elif path == '/save':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self._send(400, json.dumps({"ok": False, "error": "bad json"}).encode('utf-8'), 'application/json')
                return
            with settings_lock:
                current = dict(settings)
            save_warnings = []
            new_settings = sanitize_settings(body, current, save_warnings)
            hz_changed = new_settings["band_hz"] != current["band_hz"]
            flow_changed = any(new_settings[k] != current[k] for k, _, _ in FLOW_CLAMPS)
            with settings_lock:
                settings.update(new_settings)
            if flow_changed:
                # Live-apply; no state reset needed - the envelope and peak
                # reference remain valid, the new dynamics just take over
                # from the current values.
                apply_flow_settings(new_settings)
            if hz_changed:
                apply_band_hz(new_settings["band_hz"])
                # The adaptive references were learned against the OLD bin
                # ranges - a much narrower band sums far less raw energy, so
                # a stale section_peak would gate out every real hit for its
                # ~15s half-life and a stale level_peak_ref would pin that
                # band's flow near 0 for ~30s. Reset them; they re-learn
                # within a few frames of music.
                global section_peak, level_peak_ref
                section_peak = np.full(N_BANDS, 1e-6)
                level_peak_ref = np.full(N_BANDS, 1e-6)
                print(f"Band ranges changed to {new_settings['band_hz']}, bins {BAND_BINS}", flush=True)
            ok, msg = save_options_to_supervisor(new_settings)
            # `settings` = what was ACTUALLY saved (rejected fields fell back),
            # `warnings` = why anything was rejected - the page re-syncs its
            # controls from `settings` so it can never display rejected values
            # as if they took effect.
            self._send(200, json.dumps({"ok": ok, "error": None if ok else msg,
                                        "warnings": save_warnings,
                                        "settings": new_settings}).encode('utf-8'), 'application/json')
        elif path == '/test':
            # Fire a single manual event for one band - lets the user verify
            # each light's automation wiring on demand, with or without music.
            # (Replaces the old global "test mode" that looped fake kicks.)
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length))
                band = next(b for b in BANDS if b["name"] == body.get("band"))
            except Exception:
                self._send(400, json.dumps({"ok": False, "error": "unknown band"}).encode('utf-8'), 'application/json')
                return
            print(f"TEST: manual {band['name']} flash", flush=True)
            # Carries every field a REAL onset carries (incl. hardness) - the
            # whole point of the button is validating automation wiring, and a
            # template reading a field real events have must not error on the
            # one event the user fires while debugging.
            enqueue_event(band["event_type"],
                          {"type": "beat", "intensity": band["intensity"], "band": band["name"],
                           "strength": 0.85, "hardness": 0.85, "expected": False,
                           "ts": time.time() * 1000})
            # Every band now also has a continuous LEVEL (flow) output, and a
            # light may be wired to that instead of (or as well as) the onset
            # event - send a full-bright level too so the test exercises that
            # wiring for whichever band. The next real music level takes over.
            idx = BANDS.index(band)
            enqueue_event(LEVEL_EVENT_TYPES[idx],
                          {"type": "level", "band": band["name"], "level": 1.0,
                           "brightness": 255, "ts": time.time() * 1000})
            self._send(200, json.dumps({"ok": True}).encode('utf-8'), 'application/json')
        else:
            self._send(404, b'{}', 'application/json')

    def _send(self, code, body, content_type):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

def start_web_server():
    port = 8099
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), IngressHandler) as httpd:
        print(f"Ingress web UI listening on :{port}", flush=True)
        httpd.serve_forever()

threading.Thread(target=start_web_server, daemon=True).start()

# ---------------------------------------------------------------------------
# Audio capture - direct from a parec subprocess, NOT PortAudio/sounddevice.
# We confirmed PulseAudio itself delivers audio promptly (a raw parec test
# got ~80% of expected bytes in a 5s window - basically real-time, accounting
# for subprocess startup), but sounddevice's PortAudio<->Pulse ALSA bridge
# was only firing ~1-5 times per 5s instead of the expected ~117. Reading
# raw PCM straight from parec's stdout sidesteps that broken bridge entirely.
# ---------------------------------------------------------------------------
CHANNELS = 2
BYTES_PER_SAMPLE = 2  # s16le
CHUNK_BYTES = BLOCKSIZE * CHANNELS * BYTES_PER_SAMPLE

def read_exact(stream, n):
    # stream is an unbuffered raw pipe (bufsize=0): a single read() call may
    # return fewer than n bytes even though the pipe is healthy and more data
    # is on the way, so short reads must be accumulated, not treated as EOF.
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)

def audio_capture_loop():
    while not shutdown_requested:
        print("Starting parec capture process...", flush=True)
        proc = subprocess.Popen(
            ['parec', '--raw', f'--rate={SAMPLE_RATE}', '--format=s16le', f'--channels={CHANNELS}',
             '--latency-msec=20'],
            stdout=subprocess.PIPE,
            bufsize=0,
        )
        try:
            while not shutdown_requested:
                raw = read_exact(proc.stdout, CHUNK_BYTES)
                if raw is None:
                    print("parec stream ended - restarting capture.", flush=True)
                    break
                samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                process_block(samples.reshape(-1, CHANNELS))
        except Exception as e:
            print(f"Audio capture loop error: {e}", flush=True)
        finally:
            # Runs on every exit path, including a shutdown signal mid-read,
            # so parec never gets left running past this process's lifetime.
            proc.terminate()
            proc.wait()
        if shutdown_requested:
            break
        time.sleep(1)  # brief pause before retrying if the stream dies

# ---------------------------------------------------------------------------
# Audio-stall watchdog. The Supervisor's watchdog (config.json) health-checks
# TCP 8099 - the web UI thread - which stays green even if the AUDIO pipeline
# dies: if parec wedges with its pipe open but silent, read_exact() blocks
# forever, process_block() never runs again, and even the behind-realtime
# warning can't fire because it lives INSIDE the function that stopped being
# called. Observable symptom: UI up, watchdog green, lights dead.
#
# This thread watches last_block_mono (bumped by every processed block) from
# OUTSIDE the audio path and hard-exits the process if blocks stop flowing;
# the Supervisor watchdog then restarts the addon. A silent-but-healthy input
# can't false-trigger this: PulseAudio delivers a continuous stream of zero
# samples for a silent source, so blocks keep flowing regardless of content.
# os._exit (not sys.exit) because sys.exit in a thread only kills that
# thread - and the whole point is that the main thread is wedged in a read.
# ---------------------------------------------------------------------------
AUDIO_STALL_EXIT_S = 60.0

def audio_stall_watchdog():
    while not shutdown_requested:
        time.sleep(5)
        stalled_s = time.monotonic() - last_block_mono
        if stalled_s > AUDIO_STALL_EXIT_S and not shutdown_requested:
            print(f"FATAL: no audio blocks processed for {stalled_s:.0f}s - "
                  f"audio pipeline is wedged. Exiting so the Supervisor "
                  f"watchdog restarts the addon.", flush=True)
            os._exit(1)

threading.Thread(target=audio_stall_watchdog, daemon=True).start()

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
print("Beat detector running", flush=True)
audio_capture_loop()
print("Beat detector stopped cleanly.", flush=True)
sys.exit(0)
