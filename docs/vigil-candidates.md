# Candidates for Vigil

The rule (PLAN.md §2): QA records what belongs to a job or decides the print result (process data
with time, layer and coordinate); machine condition independent of jobs belongs to
[dwc-vigil](https://github.com/Meltingplot/dwc-vigil). While building QA, these came up on Vigil's
side of that line. QA keeps its job-bound part of each; nothing here is implemented in QA.

Checked against dwc-vigil @ 50a6e74 (`dsf/vigil_tracker.py`, 2026-09-27) — Vigil has none of them
yet, unless noted.

## 1. Full-load time relative to `maxPwm`

Vigil counts `full_load_seconds` while the raw `avgPwm ≥ 0.95` (`vigil_tracker.py`, thermal
tracking). A heater whose power is capped with `M307 … A… C… D… S<maxPwm>` below 0.95 never reaches
that, so its full-load time stays 0 however hard it works. QA's heater load (PLAN.md §5.4.1) is
`avgPwm / model.maxPwm`.

**Proposal:** count while `avg_pwm / heaters[i].model.max_pwm ≥ 0.95` (guard `max_pwm > 0`). On the
CHX 350 every heater has `maxPwm` 1 today (object model 2026-09-26), so the numbers would not
change there now; they would stay right when a power limit is configured.

## 2. Fault and warning counts independent of jobs

QA writes heater faults, driver errors/warnings/stalls and supply dips only as job events with job
context. The job-independent count and history is Vigil's (PLAN.md §2, "Grenzfälle"), and Vigil
does not count them yet. Sources (the first two as QA reads them, `dsf/qa_collector.py`):

- `heat.heaters[i].state == fault`;
- RRF events in `messages[]`: `heater-fault`, `driver-error`, `driver-warning`, `driver-stall`
  (wiki Events.md; RRF prints them only when no `<event>.g` handler exists — `GCodes::ProcessEvent`);
  main-board drivers also through new bits of `boards[0].drivers[].status` (expansion boards do not
  report status changes on their own, wiki CAN_limitations.md);
- CAN connection losses: `expansion-timeout` and `expansion-reconnect` (RRF 3.7, wiki Events.md
  and M959), with the board's CAN address. QA does not record these at all so far.

**Proposal:** lifetime/service counters per heater, per driver (board + driver) and per CAN board,
plus the date of the last occurrence.

## 3. Filament-monitor AGC drift

`sensors.filamentMonitors[i].agc` of the rotating-magnet monitor rises with the gap between magnet
and sensor; 50–105 is normal, towards 128 the gap is too large, e.g. loose screws (wiki
Sensors_filament.md and Magnetic_Encoder.md, 2026-09-27). QA stores it as a channel during jobs; the
slow drift over weeks is machine condition. dsf-python 3.7.0b1 lacks the property — QA adds it in
`dsf/qa_patches.py` (`KNOWN_PROPERTY_ADDITIONS`), Vigil would need the same patch.

**Proposal:** daily min/max/mean of `agc` per monitor next to Vigil's other vitals.

## 4. Resonance check at rest

QA records accelerometer spectra while printing (phase 6), so they depend on the moves of the job.
A spectrum of the bare machine under the same moves every time (after homing, e.g. weekly) would
show belt tension and loose parts independent of what is printed. That needs a maintenance macro
with defined moves and M956, which is outside passive recording; Vigil's service log would be the
place for its result.

**Proposal:** a service action "resonance check" in Vigil that runs a fixed macro, stores the
spectrum's peak and RMS per axis and compares it with the previous check. The spectrum can be
computed like QA's `dsf/qa_accel.py` (identical to DWC's input-shaping plugin, no numpy).

## 5. Camera availability

QA fetches the HMI camera snapshot only while a job prints and notes failures per job
(`timelapse_failed`). Whether the camera is up at all is HMI condition.

**Proposal (low priority):** Vigil probes `timelapse.snapshotUrl` (or a configured URL) every few
minutes and counts failures per day.
