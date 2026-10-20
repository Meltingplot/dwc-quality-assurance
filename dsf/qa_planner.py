"""RRF's motion planning, so the replay's fusion view can estimate the flow RRF planned for the moves the object
model journal has no sample of (Tim 2026-10-20). DSF reports the current move about every 130 ms, so most short
moves of a layer have none; their flow differs from the G-code's mostly because they are too short to reach their
feed rate, which a look-ahead over the G-code reproduces.

What RRF does, read in RRF 3.7-dev @ ea93885 (2026-10-20), without S-curve (``move.IsUsingSCurve`` off):

- Requested speed: F × M220 factor, at least ``minSpeed``, within the box of the axis and extruder speed limits
  and the kinematics' motor limits (DDA.cpp:563-590, CoreKinematics.cpp:368-404).
- Acceleration: M204 P (printing) or T (travel) for an XY move, within the box of the M201 axis and extruder
  accelerations and the motor limits; CoreXY's motors move X+Y and X−Y (DDA.cpp:540-548, CoreKinematics.cpp:214-220).
- Junction (DDA_3rdOrder.cpp:37-110): two printing moves meld when neither retracts and the extrusion per mm
  changes by no more than a factor 5 (the start speed follows the ratio, so the extruder keeps its speed); two
  travel moves meld; anything else stops (jerk policy 0, M566 P). The previous move ends at most at the slower
  requested speed and within the maximum jerk (M566) of each axis; the look-ahead then holds the per-drive speed
  change within the printing jerk (M205, at most M566; ``DDA::MatchSpeeds`` DDA.cpp:1116-1131, Move2.cpp:182-193).
- Look-ahead: a move must be able to slow to the next one's start; top speed V² = (2aL + u² + v²) / 2, at most
  the requested speed (``DDA::RecalculateMove`` DDA.cpp:998-1110).
- Codes that wait for standstill end the chain (M201, M400, G4, macros, …); M204, M205, M566, M106, M220, M221,
  M486, M572 do not (GCodes2.cpp, 2026-10-20).

Checked against the journal of Benchy 20261008-120236 (CHX 350, 21 815 sampled printing moves): the planned top
speed was within 0.4 % of RRF's for 90 % of the moves, 0.7 % for 95 %; the misses are moves of about 0.1 mm at
over 100 mm/s, where RRF plans slower. Not modelled: S-curve, RRF's segmentation (mesh compensation, kinematics),
the limited depth of RRF's look-ahead, jerk policy 1 across move types.
"""

import math
import statistics

# Codes that do not stop the motion between two moves (verified in RRF 3.7-dev @ ea93885 GCodes2.cpp, 2026-10-20:
# none of them waits for standstill); any other code ends the chain
NON_BLOCKING = frozenset((("G", 90), ("G", 91), ("M", 73), ("M", 82), ("M", 83), ("M", 106), ("M", 107),
                          ("M", 117), ("M", 118), ("M", 203), ("M", 204), ("M", 205), ("M", 220), ("M", 221),
                          ("M", 486), ("M", 566), ("M", 572), ("M", 593)))
MAX_RATIO_CHANGE = 0.2   # a printing move whose extrusion per mm falls below this share of the previous one's stops
CORE_XY = ("corexy", "corexyu", "corexyuv")
INF = float("inf")


def blocks(words):
    """Whether a code that is not a G0-G3 move stops the motion (``words`` from ``qa_gcode.parse_words``)."""
    for letter in ("G", "M"):
        if letter in words:
            return (letter, int(words[letter])) not in NON_BLOCKING
    return True  # T-codes and anything else


def _positive(value, default):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else default


class Limits:
    """The machine's limits from the object model's ``move`` (units as RRF reports them: mm/s², mm/min)."""

    def __init__(self, move=None):
        move = move if isinstance(move, dict) else {}
        axes = {a.get("letter"): a for a in move.get("axes") or [] if isinstance(a, dict)}
        extruder = next((e for e in move.get("extruders") or [] if isinstance(e, dict)), {})
        drives = {letter: axes.get(letter, {}) for letter in "XYZ"}
        drives["E"] = extruder
        self.acc = {k: _positive(d.get("acceleration"), INF) for k, d in drives.items()}
        self.speed = {k: _positive(d.get("speed"), INF) / 60 for k, d in drives.items()}
        self.max_jerk = {k: _positive(d.get("jerk"), INF) / 60 for k, d in drives.items()}
        self.jerk = {k: min(_positive(d.get("printingJerk"), INF) / 60, self.max_jerk[k]) for k, d in drives.items()}
        self.acc_print = _positive(move.get("printingAcceleration"), INF)
        self.acc_travel = _positive(move.get("travelAcceleration"), INF)
        self.speed_factor = _positive(move.get("speedFactor"), 1.0)
        self.extrude = _positive(extruder.get("factor"), 1.0)
        self.min_speed = _positive(move.get("minSpeed"), 0.0) / 60
        self.core_xy = str((move.get("kinematics") or {}).get("name") or "").lower() in CORE_XY


class Move:
    """One G0/G1 piece with X/Y travel: unit direction (X, Y, Z), length, extrusion per mm (M221 applied), requested
    speed and acceleration (mm/s, mm/s²), printing and maximum jerk per drive (mm/s)."""

    __slots__ = ("pos", "printing", "ux", "uy", "uz", "length", "ez", "req", "acc", "jerk", "max_jerk")

    def __init__(self, pos, dx, dy, dz, e, feed, motion, limits):
        self.pos = pos
        self.length = math.sqrt(dx * dx + dy * dy + dz * dz)
        self.ux, self.uy, self.uz = dx / self.length, dy / self.length, dz / self.length
        extrude = motion.get("extrude") or limits.extrude
        self.printing = e > 0
        self.ez = e * extrude / self.length if self.printing else 0.0
        parts = {"X": abs(self.ux), "Y": abs(self.uy), "Z": abs(self.uz), "E": self.ez}
        acc = {k: motion.get("acc" + k) or limits.acc[k] for k in "XYZE"}
        if self.printing:
            a = motion.get("accP") or limits.acc_print
        else:
            a = motion.get("accT") or limits.acc_travel
        speed = feed * (motion.get("speed") or limits.speed_factor)
        speed = max(speed, limits.min_speed)
        for k, part in parts.items():
            if part > 0:
                a = min(a, acc[k] / part)
                speed = min(speed, limits.speed[k] / part)
        if limits.core_xy:
            for motor, k in ((abs(self.ux + self.uy), "X"), (abs(self.ux - self.uy), "Y")):
                if motor > 0:
                    a = min(a, acc[k] / motor)
                    speed = min(speed, limits.speed[k] / motor)
        self.req = speed
        self.acc = a if math.isfinite(a) else 1e9
        max_jerk = {k: motion.get("maxJerk" + k) or limits.max_jerk[k] for k in "XYZE"}
        self.max_jerk = max_jerk
        self.jerk = {k: min(motion.get("jerk" + k) or limits.jerk[k], max_jerk[k]) for k in "XYZE"}


def _junction(a, b):
    """``(speed, ratio)``: the highest speed move ``a`` may end at for ``b`` to follow without a stop (0: it
    stops), and the ratio of ``b``'s start speed to it."""
    if a.printing != b.printing:
        return 0.0, 1.0
    ratio = 1.0
    if b.printing:
        ratio = b.ez / a.ez
        if ratio < MAX_RATIO_CHANGE:
            return 0.0, 1.0
    # the limits in force when RRF adds ``b`` (and looks ahead from it)
    speed = min(b.req / ratio, a.req)
    for k, ua, ub in (("X", a.ux, b.ux), ("Y", a.uy, b.uy), ("Z", a.uz, b.uz)):
        change = abs(ratio * ub - ua)
        if speed * change > b.max_jerk[k]:
            speed = b.max_jerk[k] / change
    # DDA::MatchSpeeds: the change of each drive's speed within its printing jerk, extruder included
    for k, ua, ub in (("X", a.ux, b.ux), ("Y", a.uy, b.uy), ("Z", a.uz, b.uz), ("E", a.ez, b.ez)):
        change = abs(ua - ub)
        if speed * change > b.jerk[k]:
            speed = b.jerk[k] / change
    return speed, ratio


def plan(moves):
    """Top speed (mm/s) RRF plans for each move; ``moves`` in file order, None where the motion stops (a code
    that waits, a move without X/Y travel). None for those entries."""
    n = len(moves)
    junction = [0.0] * n
    ratio = [1.0] * n
    for i in range(1, n):
        if moves[i] is not None and moves[i - 1] is not None:
            junction[i], ratio[i] = _junction(moves[i - 1], moves[i])
    # backward: a move ends no faster than the next one can start and still slow down to its own end
    end = [0.0] * n
    for i in range(n - 2, -1, -1):
        if moves[i] is not None and junction[i + 1] > 0:
            b = moves[i + 1]
            end[i] = min(junction[i + 1], math.sqrt(end[i + 1] ** 2 + 2 * b.acc * b.length) / ratio[i + 1])
    # forward: and no faster than it can accelerate to from its start
    tops = [None] * n
    start = 0.0
    for i, m in enumerate(moves):
        if m is None:
            start = 0.0
            continue
        finish = min(end[i], math.sqrt(start ** 2 + 2 * m.acc * m.length))
        tops[i] = min(m.req, math.sqrt((2 * m.acc * m.length + start ** 2 + finish ** 2) / 2))
        start = finish * ratio[i + 1] if i + 1 < n and junction[i + 1] > 0 else 0.0
    return tops


def correction(sampled, planned):
    """The median of the sampled over the planned flow of the moves that have both (``{pos: mm³/s}`` each):
    what the plan misses for the whole layer, e.g. a changed M221. None without such a move."""
    ratios = [sampled[pos] / planned[pos] for pos in sampled if planned.get(pos, 0) > 0 and sampled[pos] > 0]
    return round(statistics.median(ratios), 4) if ratios else None
