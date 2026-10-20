"""RRF's look-ahead for the replay's fusion view (qa_planner): the cases of DDA.cpp RecalculateMove and the
junction rules of DDA_3rdOrder.cpp / DDA::MatchSpeeds (RRF 3.7-dev @ ea93885, 2026-10-20)."""

import math

import pytest

import qa_planner

MODEL = {
    "axes": [{"letter": "X", "acceleration": 6000, "jerk": 720, "printingJerk": 600, "speed": 60000},
             {"letter": "Y", "acceleration": 6000, "jerk": 720, "printingJerk": 600, "speed": 60000},
             {"letter": "Z", "acceleration": 400, "jerk": 72, "printingJerk": 72, "speed": 1800}],
    "extruders": [{"acceleration": 3000, "jerk": 1800, "printingJerk": 1800, "speed": 2400, "factor": 1}],
    "printingAcceleration": 1000, "travelAcceleration": 2000, "speedFactor": 1, "minSpeed": 30,
    "kinematics": {"name": "cartesian"},
}


def move(dx, dy, e=0.0, feed=100.0, motion=None, limits=None, pos=0):
    return qa_planner.Move(pos, dx, dy, 0.0, e, feed, motion or {}, limits or qa_planner.Limits(MODEL))


def test_limits_in_rrf_units():
    limits = qa_planner.Limits(MODEL)
    assert limits.jerk["X"] == 10 and limits.max_jerk["X"] == 12   # mm/min in the model
    assert limits.speed["E"] == 40 and limits.acc["E"] == 3000
    assert (limits.acc_print, limits.acc_travel, limits.min_speed) == (1000, 2000, 0.5)
    assert qa_planner.Limits(None).acc_print == math.inf


def test_a_long_move_reaches_its_speed_a_short_one_does_not():
    assert qa_planner.plan([move(100, 0, 1)]) == [100]
    # from standstill to standstill: V² = (2aL + 0 + 0) / 2
    assert qa_planner.plan([move(2, 0, 0.02)])[0] == pytest.approx(math.sqrt(1000 * 2))


def test_moves_in_line_meld_a_corner_slows_to_the_jerk():
    # 6 mm without a stop: up for 3 mm, down for 3 mm, the peak in the middle move
    straight = qa_planner.plan([move(2, 0, 0.02), move(2, 0, 0.02), move(2, 0, 0.02)])
    assert straight == pytest.approx([math.sqrt(1000 * 4), math.sqrt(1000 * 6), math.sqrt(1000 * 4)])
    # 90°: each axis changes by the whole speed, within the printing jerk of 10 mm/s
    corner = [move(50, 0, 0.5), move(0, 50, 0.5)]
    first, second = qa_planner.plan(corner)
    assert qa_planner._junction(*corner) == (pytest.approx(10), 1.0)
    assert first == second == 100


def test_what_stops_the_motion():
    printing, travel = move(1, 0, 0.01), move(1, 0)
    assert qa_planner._junction(printing, travel)[0] == 0          # printing ↔ travel (jerk policy 0)
    assert qa_planner._junction(move(1, 0, 0.01), move(1, 0, 0.001))[0] == 0   # extrusion per mm falls below 1/5
    melded = qa_planner._junction(move(1, 0, 0.01), move(1, 0, 0.005))
    assert melded[1] == pytest.approx(0.5)                          # the extruder keeps its speed
    # a code that waits: the move before it ends, the one after it starts at standstill
    assert qa_planner.plan([move(2, 0, 0.02), None, move(2, 0, 0.02)])[0] == pytest.approx(math.sqrt(2000))
    assert qa_planner.blocks({"M": 400.0}) and qa_planner.blocks({"G": 10.0}) and qa_planner.blocks({"T": 0.0})
    assert not qa_planner.blocks({"M": 204.0, "P": 500.0}) and not qa_planner.blocks({"M": 106.0, "S": 1.0})


def test_accelerations_and_speeds_of_the_move():
    assert move(1, 0, 0.01).acc == 1000 and move(1, 0).acc == 2000   # M204 P, T
    assert move(1, 0, 0.01, motion={"accP": 500, "speed": 0.5}).acc == 500
    assert move(1, 0, 0.01, feed=100, motion={"speed": 0.5}).req == 50   # M220
    assert move(1, 0, 1.0, feed=100).req == 40                       # the extruder's 40 mm/s
    assert move(1, 0, 0.01, motion={"extrude": 1.1}).ez == pytest.approx(0.011)
    assert move(1, 0, 0.01, motion={"jerkX": 20}).jerk["X"] == 12    # M205 at most the M566 maximum
    core = qa_planner.Limits({**MODEL, "kinematics": {"name": "coreXYU"}, "printingAcceleration": 6000})
    # CoreXY: the motors move X+Y and X−Y, so a diagonal gets 6000 / √2 (CHX 350 journal: 4242.6)
    assert move(1, 1, 0.01, limits=core).acc == pytest.approx(6000 / math.sqrt(2))
    assert move(1, 0, 0.01, limits=core).acc == 6000


def test_correction_is_the_median_ratio():
    assert qa_planner.correction({1: 2.0, 2: 3.3, 3: 1.0}, {1: 2.0, 2: 3.0, 3: 0}) == pytest.approx(1.05)
    assert qa_planner.correction({1: 2.0}, {}) is None
