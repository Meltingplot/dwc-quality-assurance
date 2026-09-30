# HTTP API

Base path `/machine/QualityAssurance/`. Parameters are query strings (DSF matches endpoint paths
exactly). JSON everywhere except `job/export` (a file). Times in the database and in `ts_ms`
fields are epoch milliseconds; `…At`/`timestamp`/`ts` fields are ISO 8601 UTC.

Contract tests: `tests/test_api.py` (Python side), including the fields the CHX UI uses.

## Authentication

Every endpoint needs a DWC session: requests without one get **401**
`{"error": "a DWC session is required"}`, the `live` socket is closed with **1008**. DSF does not
check sessions on plugin endpoints itself; it only looks the key up and passes the session id
(−1 for none) to the daemon, which refuses −1 (`qa_api.call`, `make_live_handler`; DSF v3.7-dev
@ cd3ae65f `CustomEndpointMiddleware.cs`, 2026-09-27).

- HTTP: the `X-Session-Key` header. DWC's REST connector sends it on every request, so go through
  `useMachineStore().request(...)`, not `fetch()`. Files (export, timelapse video and frames) are
  requested with `responseType: "blob"`; a bare `<a href>`/`<img src>`/`<video src>` has no key.
- WebSocket: `?sessionKey=<key>` in the URL, then one text frame from the client (DSF hands the
  daemon the session only with client frames). `hello` comes only after that.
- A server-side caller (a later Quality Control daemon) needs a session too: `GET
  /machine/connect?password=…` returns `sessionKey`. DSF drops a session after 8 s without a
  request unless a WebSocket of it is open (`SessionTimeout`, default 8000 ms;
  `SessionStorage.MaintainSessions`; DSF v3.7-dev @ cd3ae65f, 2026-09-28). A dropped key is looked
  up as −1, so QA answers 401. Every request with the key keeps it alive, plugin endpoints
  included (`GetSessionId` refreshes it); after a pause, `GET /machine/noop` or connect again.
- Only a machine with a password (M551) is protected by this: without one, `/machine/connect`
  gives every caller a key.

Every request other than `live` goes through the HMI's haproxy backend that gives a busy DSF
10 s (PLAN.md §5.6): every endpoint answers from indexed data; the G-code layer index is built in
the background at job start (`job/toolpath` answers 202 until it is ready).

| Method | Path | Parameters | Answer |
|---|---|---|---|
| GET | `status` | – | daemon, collector, database, timelapse state |
| GET | `settings` | – | `{settings, errors}` |
| POST | `settings` | body: settings object (partial is fine, ≤ 32 KiB) | always 200: `{saved, settings, errors}`; nothing is stored when `errors` is not empty (DWC's REST connector drops the body of a 4xx answer) |
| GET | `jobs` | `limit` (50, ≤ 1000), `offset`, `result` (`finished`/`completed`, `cancelled`, `aborted`, `running`, `unknown`), `material` | `{total, offset, jobs: [JobEntry]}` newest first |
| GET | `job` | `id` | JobEntry + `context`, `summary`, `fileCrc32`, `startLayer`, `durationS`, `warmupS`, `pauseS`, `journal` (`{bytes, start, end, snapshots}` of the object model journal, null without one) |
| GET | `job/layers` | `id` | `{jobId, meta, layers: [Layer]}` |
| GET | `job/events` | `id`, `type` (comma list) | `{jobId, events: [Event]}` |
| GET | `job/samples` | `id`, `channels` (comma list, ≤ 50), `from`, `to` (epoch ms), `resolution` (`coarse`, `fine`, `auto`) | `{jobId, from, to, resolution, downsampled, channels: {name: [[ts_ms, value]]}}` |
| GET | `job/blocks` | `id` | `{jobId, blocks: [{id, start_ms, end_ms, triggers}]}` |
| GET | `job/spectra` | `id` | `{jobId, spectra: [Spectrum]}`, see "Spectra" |
| GET | `job/toolpath` | `id`, `layer` | segments of the layer; 202 while the index is built; 409 when the file is gone or changed |
| GET | `job/toolpath/stack` | `id`, `from` (layer, default the first) | the replay's 3D stack, reduced, in pieces: see "Toolpath stack"; 202 and 409 as `job/toolpath` |
| GET | `job/export` | `id` | the job as one JSON file (`application/octet-stream`) |
| GET | `job/om` | `id`, `at` (epoch ms, default the journal's end), `path` (e.g. `move.compensation`, `boards[2].drivers[0]`; default the whole model) | `{at, snapshotAt, path, value}`: the object model or the value at `path` as it was then, see "Object model journal"; 404 without a journal |
| GET | `job/om/history` | `id`, `path`, `from`, `to` (epoch ms), `limit` (1000, ≤ 10000) | `{path, points: [{t, value}], truncated}`: the value at `from` (or at the first snapshot), then every change; `path` `messages` gives each new message |
| GET | `job/om/journal` | `id` | the journal as one gzip file of JSON lines (`application/octet-stream`) |
| GET | `job/timelapse/meta` | `id` | status and layer → frame index, see "Timelapse" |
| GET | `job/timelapse` | `id` | the AV1/MP4 video (`application/octet-stream`, no Range); 404 until it is verified |
| GET | `job/timelapse/frame` | `id`, `layer` | JPEG of the layer's frame (`application/octet-stream`); 404 when the layer has none |
| GET | `trends` | `metric`, `limit` (100), `material` | `{metric, points: [...]}` newest first |
| GET | `channels` | – | `{channels: [...], derived: [...]}` |
| GET/POST | `spectra/reference` | POST body `{axis, spectrumId}` or `{axis, mode: "auto"}` | `{references: [Reference], autoCount}`, see "Spectra" |
| GET | `spectra/latest` | `axis` (`X`/`Y`/`Z`), `limit` (5, ≤ 50) | `{axis, spectra: [Spectrum]}`: the newest spectrum of each of the last jobs, newest first |
| WebSocket | `live` | – | frames, see below |

Errors: `{"error": "..."}` with 400 (bad parameter), 404 (unknown job/layer), 409, 500. Through DWC's REST connector a
4xx arrives as `FileNotFoundError` (404) or `OperationFailedError("bad status code <n>")` without the body.

## JobEntry (list) — the CHX UI history contract

`useJobHistory.ts` reads the first six fields (`HistoryEntry` + `analysable`):

| Field | |
|---|---|
| `id` | job id `YYYYMMDD-HHMMSS-<crc32 hex>` (UTC) |
| `file` | virtual path of the job file |
| `result` | `running`, `finished`, `cancelled`, `aborted`, or `unknown` (the daemon was not running when the job ended) |
| `printTimeS` | job duration from RRF (last `job.duration` of the job), null while running |
| `timestamp` | end time, start time while running |
| `analysable` | always `true` |
| `qaResult` | QA's own result: `running`, `completed`, `cancelled`, `aborted`, `unknown` |
| `startedAt`, `endedAt`, `partial`, `numLayers`, `material`, `rawPruned` | |
| `summary` | excerpt: `abortReason`, `layers`, `events` (count per type), `filamentRatio` and `avgPercentage` per monitor, `heaterLoadMean` and `heaterLoadEvents` per nozzle heater, `spoolUsageG` per tool |

`partial`: the daemon started while this job was already running (records begin at `startLayer`).
`rawPruned`: samples and blocks were removed by retention; summary, layers and events remain.

## Layer — the CHX UI analysis contract

`meta`: `sensors` `[{index, name, type}]`, `heaters` `[{index, role (nozzle|bed|chamber|other), tool,
sensor, sensorName}]`, `chamber` `["sensor"|"heater", index]` or null, `filamentDiameters`,
`heaterLoad` thresholds `{high, limit}`. `gcode`: state of the file's layer index, `ready`, `building`
(ask again), `missing`, `gone` (file deleted before an index was built), `changed` (file rewritten
since the job) or `error: …`.

Each layer (`job.layer` numbering, i.e. RRF's):

| Field | |
|---|---|
| `layer`, `startedAt`, `endedAt`, `durationS` | |
| `height` | layer thickness from DSF's `job.layers[]` when available, else null |
| `z` | machine Z of the last extruding sample of the layer (cumulative height) |
| `fractionPrinted` | |
| `filament` | per extruder/monitor `{commandedMm, measuredMm, extruderMm, ratio}`: commanded = Δ monitor `totalExtrusion` over its restarts; measured = commanded × the monitor's `lastPercentage`, weighted per check segment (RRF reports no measured distance, see "Channels"); `ratio` = measured / commanded; extruder = Δ `move.extruders[].position` |
| `flow` | per extruder, mm³/s: `extruderMm` × cross-section / layer duration (not the monitor's total, which advances one check segment at a time and left small layers at 0) |
| `temps` | `heaters` per heater index `{min, max, mean, std, setpoint}`, `sensors` per analog sensor index `{…, name}` (not compact, unlike `job.layers[].temperatures`), `chamber` `{min, max, mean, std}` |
| `fmStats` | per monitor: `lastPercentage` `{min, max, mean, std}`, `avgPercentage`, `mmPerRev` at layer end |
| `pwmStats` | per heater at a constant, reached setpoint: `avgPwmMean`, `avgPwmStd`, `currentStd` |
| `loadStats` | per heater: `mean`, `max`, `p95` (load at the setpoint), `maxMean60`, `atSetpointS`, `shareAtSetpoint`, `shareHigh`, `shareLimit` (time share of the 60 s mean ≥ high/limit), `setpoint` |
| `gcode` | from the file's layer index (absent unless `meta.gcode` is `ready`, null for a layer the file does not have): `extrudeMm` (filament of printing moves), `types` (the same per `;TYPE:`), `retracts` (retractions: negative E or G10/M103, once until E moves forward or G11/M101), `fwRetracts` (of these firmware ones, each the tool's M207 length: context `tools[].retraction`, changes as `setpoint_change` `tool.retraction`), `retractMm` (negative E), `macros` (`M98 P` calls: path → count; their own moves are not in the file), `pathMm` (E travel of the file's moves both ways, Σ\|ΔE\|), `macrosRetracted` (of the calls, those made while retracted) |
| `filamentPath` | the filament's path through the extruder gear, counted when the layer ends with the e-steps and M207 valid at its start (null: no index, or the file lacks the layer): `mm` (path both ways: the file's E, firmware retraction cycles of length + length + extraRestart, the macros' own E as in `context.macros`), `netMm` (filament fed), `gearPasses` (`mm` / `netMm`: 1 without retraction, 3 when every piece went back and forth once; null when `netMm` < 0.1), `stepsPerMm`, `motorSteps` (`mm` × `stepsPerMm`), `retraction` `{length, extraRestart}`, `unknownMacros` (called, but unreadable) |
| `feed` | per extruder that fed filament in the layer (absent in layers recorded before schema version 3): filament pushed per millimetre of the file, relative to the job's start: `factor` (e-steps / `reference` × extrusion factor, over the extruder's forward movement with the values that held meanwhile; 1.0417 after the MFM's M92 801 → 834.38), `min`, `max` (of the factor while feeding), `stepsPerMm` and `extrusionFactor` (M221) as they held for the fed filament, `reference` (context `feedReference`) |

Heater load = `avgPwm / model.maxPwm`, clamped to 0…1; counted only while processing, at an
active setpoint that was reached (the CHX UI banner's definition, PLAN.md §5.4.1).

## Event

`{id, ts_ms, ts, end_ms, type, subtype, layer, x, y, z, positions, workplace, offsets, tool,
object_id, device, payload, block_id}`. `positions`/`x,y,z` are machine positions; `workplace`
and `offsets` (that workplace's offsets per axis) allow converting to user coordinates. `device`
is the heater, monitor, board or driver number the event is about. Ongoing conditions get
`end_ms` and `payload.durationS` when they end.

| type | subtype | payload |
|---|---|---|
| `job_start` | – | `file`, `crc32`, `partial` |
| `job_end` | result | `result`, `abortReason` |
| `daemon_started_mid_job` | `resumed` / `new` | `resumed`, `lastRecordMs` / `layer` |
| `pause` | cause (`filament_monitor`, an event type, `message: <title>`, `user`) | `cause` |
| `resume` | – | `pausedS`, `pauseEvent` |
| `filament_status` | new status | `monitor`, `status`, `from`, `lastPercentage`; end: `returnedTo`, `durationS` |
| `filament_percent_window` | `low` / `high` | `monitor`, `since`, `percentMin`, `percentMax`, `lastPercentage`, `side`; end: `extreme` |
| `filament_percent_level` | `low` / `high` | `lastPercentage` ≥ `thresholds.filamentLevelPoints` (default 20) away from the monitor's own level in the job (time-weighted median of its readings so far, in 2 % classes, once it has `filamentLevelMinS` of them) for `filamentPercentWindowMinS`, until a reading is back within the threshold; a null reading changes nothing: `monitor`, `since`, `level`, `threshold`, `lastPercentage`, `side`; end: `extreme`, `returnedTo` |
| `filament_percent_drift` | `low` / `high` | a run of `filamentDriftLayers` (default 3) or more layers whose monitor mean (`fmStats` mean) lies `thresholds.filamentDriftPoints` (default 6) or more on one side of the monitor's level in the job (as `filament_percent_level`; a run keeps the level it started at), from the first one's start to the start of the first layer back; for a drift too slow for `filament_percent_level`: `monitor`, `level`, `threshold`, `side`, `firstLayer`, `lastLayer`, `layers`, `extreme` (the layer mean farthest from the level) |
| `gear_passes` | `high` | a run of layers with `filamentPath.gearPasses` ≥ `thresholds.gearPasses` (default 5), from the first one's start to the start of the first layer below: `threshold`, `firstLayer`, `lastLayer`, `max`, `maxLayer` |
| `gear_passes_forecast` | `high` | once per job, when a layer has started (print_start has set M207) and the layer index is ready: the file's runs of layers expected at `thresholds.gearPasses` or more, with the M207 and the macros valid then (context `gearForecast` holds every layer); none when no layer reaches it: `threshold`, `runs` `[{firstLayer, lastLayer, max, maxLayer}]`, `layers` (how many reach it), `max`, `maxLayer`, `retraction` |
| `frame_change` | `layer` / `pause` (a pause lay between the run's first frame and the one before) | a run of M240 timelapse frames whose thumbnail differs from the M240 frame before in `thresholds.frameChangePixels` (default 8) pixels or more (see "Timelapse"), from the first one's photo to the photo of the first frame below; written at the first frame, so a reader can react while the job prints: `threshold`, `firstLayer`, `lastLayer`, `max`, `maxLayer`, `box` (of the max frame); end: `durationS`, `closedBy` `job_end` when the job ended first; written by the timelapse thread, so no position, tool or object |
| `heater_fault` | – | `heater`, `previousState`, `current` |
| `heater_monitor` | `tooHigh` / `tooLow` | `heater`, `monitor`, `limit`, `reading`, `sensor`, `action` |
| `heater_load` | `high` / `limit` | `heater`, `tool`, `setpoint`, `level`, `peakMean`, `levels`, `volumetricFlow`, `speedFactor` |
| `mfm_error_tolerated` | – | `count` + the MFM globals |
| `mfm_recovery` | `requested` / `false_positive` / `real_issue` | `result` + the MFM globals |
| `mfm_flow_bias` | `detected` | the MFM globals (`mfm_esteps_suggested`, …) |
| `setpoint_change` | `heater.active`, `heater.standby`, `heater.maxPwm`, `stepsPerMm`, `pressAdv.k0/k1/d`, `nonlinear.a/b/upperLimit`, `fm.configured`, `fm.calibrated`, `tool.retraction` (M207 `{length, extraRestart, speed, unretractSpeed, zHop}`, index = tool number), `shaping` (M593 `{type, frequency, damping, amplitudes, delays}`, index null), `axis.microstepping` (M350 `{value, interpolated}`, index = axis letter), `extruder.microstepping` (the same, index = extruder) | `setpoint`, `index`, `from`, `to`; `stepsPerMm` also `cause` (`mfm_flow_bias`, `baseline_restore`, null) |
| `babystep` | – | `axis`, `from`, `to` |
| `calibration` | `mesh` (G29 probed or loaded a mesh), `levelling` (G32), `probe` (M558.1, G31; device = probe), `probeDrive` (M558.2) | the new entry as in the context's `calibration` (without the mesh's `heights`); the height map's `minError`, `maxError`, `points` and M558.1's `rmsError` are added to the event when they arrive; no fine block |
| `firmware_restart` | `during_job` / `before_job` | `state.upTime` (RRF's seconds since boot) started over: M999, a reset, a power cycle. `during_job` with a fine block, and a cause for `job_end`'s `abortReason`; `before_job`: seen while no job recorded, listed by the next job before its `job_start` at the time QA saw it, without position. `bootedAt` (epoch ms), `upTimeBefore`, `upTimeAfter` (s), `lastSeenMs` (the reading before), `detectedAt` |
| `machine_mode` | the new mode (`automatic`, `default`) | `global.machine_mode` of chx350-config changed during the job: `from`, `to`; with a fine block, and a pause cause |
| `driver_error` | `error` / `warning` / `stall` | `source` `status`: `board`, `canAddress`, `driver`, `status`, `previous`, `bits`; `source` `message`: `canAddress`, `driver`, `text` |
| `voltage_dip` | – | `board`, `vIn`, `median90s`; end: `recoveredTo` |
| `phantom_reading` | `heater` / `sensor` | `channel`, `before`, `peak`, `durationMs` |
| `timelapse_failed` | `snapshot` (first failed snapshot of a job; later ones only in the index), `encode`, `empty` (no frame at all) | `error`, `snapshot` also `layer`; written by the timelapse threads, so no position, tool or object |
| `accelerometer_failed` | `start` (M956 answered with an error), `timeout` (`runs` did not advance), `run` (RRF's reason, e.g. `Received bad data`, or `points` 0), `overflow` (samples lost), `error` | `error`; `code`, `points` or `overflows` where known; no position (written by the recorder thread) |

## Channels

| Channel | |
|---|---|
| `heater.<n>.current`, `.active`, `.standby`, `.avgPwm`, `.state` | state: 0 off, 1 standby, 2 active, 3 fault, 4 tuning, 5 offline |
| `heater.<n>.load` | derived at read time from `avgPwm` and the `maxPwm` valid then (not stored) |
| `extruder.<n>.position`, `.rawPosition`, `.factor`, `.stepsPerMm`; `job.rawExtrusion` | `stepsPerMm` since schema version 3 |
| `fm.<n>.status`, `.lastPercentage`, `.avgPercentage`, `.minPercentage`, `.maxPercentage`, `.position`, `.totalExtrusion`, `.agc`, `.calibrated.totalDistance`, `.calibrated.mmPerRev` | status: 0 ok, 1 noMonitor, 2 noDataReceived, 3 noFilament, 4 tooLittleMovement, 5 tooMuchMovement, 6 sensorError. `totalExtrusion` and `calibrated.totalDistance` are both the *commanded* filament since the monitor's calibration started (whole mm from a CAN monitor); the measured movement is only in the percentages (`avgPercentage` = 100 × measured / commanded), and a CAN monitor does not even send it. Its `calibrated.mmPerRev` = configured mmPerRev × 100 / `avgPercentage`. The percentages are null while the monitor has no live data (RRF 3.7-dev @ 32a84d2 `Duet3DFilamentMonitor.cpp:41-46, 292-304`, `RotatingMagnetFilamentMonitor.cpp:43-46, 697-711`; Duet3Expansion 3.7-dev @ 913c761 `RotatingMagnetFilamentMonitor.cpp:628-650`; the same in the Meltingplot forks; 2026-09-29) |
| `sensor.<n>.lastReading` | every analog sensor |
| `axis.<L>.machinePosition` | |
| `move.currentMove.topSpeed`, `.requestedSpeed`, `.extrusionRate`; `move.speedFactor` | of the move RRF is executing; `extrusionRate` is the filament mm/s planned for it at its top speed, not measured (RRF 3.7-dev @ 32a84d2 Move.cpp:213, DDA::GetTotalExtrusionRate DDA.cpp:1686-1694, read 2026-09-29) |
| `fan.<n>.actualValue`, `.requestedValue`, `.rpm` | rpm only with a tacho |
| `board.<n>.vIn`, `.v12`, `.mcuTemp` | `<n>` = index in `boards[]` (context `boards` has the CAN address) |
| `global.mfm_*` | the MFM globals of chx350-config (booleans as 0/1) |

`resolution`: `coarse` = the 5 s grid, `fine` = every row of the fine blocks (full object-model
resolution around a trigger), `auto` = both. More than 20 000 points per channel are thinned by
stride (`downsampled: true`).

## Toolpath

`{layer, segments: {x0, y0, x1, y1, z, e, flow, object, type, travel}, types: [...], objects: {id: name},
meta: {numLayers, source (comments|z), objects, filamentDiameter}}`, columns of equal length.
Coordinates are the G-code's (user) coordinates; `flow` in mm³/s from ΔE × cross-section / (length / feed
rate); `type` indexes `types` (slicer `;TYPE:`); `travel` 1 for moves without extrusion.

## Toolpath stack

The layers the replay's 3D view stacks below the one it shows, reduced by the daemon so that a print filling
the CHX 350's build volume (≈ 880 × 420 × 943 mm) stays small to send and to draw:
`{layers: [StackLayer], next, meta: {numLayers, source, objects, filamentDiameter, bounds, resolution, step}}`.
One answer reads at most 256 KiB of G-code (at least one layer); ask again with `from=next` until `next` is null.

- `bounds` `{minX, minY, maxX, maxY, maxZ}`: extent of the printing moves (layer index, version 4), null when
  nothing prints.
- `resolution` (mm) = the largest extent / 1000, at least 0.02: paths follow the G-code within it.
- `step`: the stack holds every `step`-th layer of the index, so layers are about one resolution apart and a
  stack reads at most 64 MiB of G-code.
- StackLayer `{layer, z, x, y, bulge, start, object}`: the extruding moves joined into paths (sparse infill left
  out: `;TYPE:` `Internal infill`, `Sparse infill`, `FILL`), each path as few vertices as keep every point
  within `resolution`, lines and arcs. `x`, `y`, `bulge` per vertex, `bulge` for the piece that ends at the
  vertex: tan of a quarter of the arc's sweep, positive counter-clockwise, 0 for a line (the centre lies
  `chord / 2 × (1 − bulge²) / (2 bulge)` to the left of the chord's middle). `start` (first vertex) and `object`
  per path. `z` is the Z of the layer's last extruding move, null when it extrudes nothing.

## Timelapse

`job/timelapse/meta`: `{jobId, status, codec, fps, frames, sizeBytes, error, video, layers: [{layer, frame, ts, reason?,
waitMs?, still?, changedPx?, changedBox?, afterPause?}]}`.
`status`: `none` (nothing recorded; `reason` says why, e.g. `no snapshotUrl set`), `capturing`, `queued`,
`encoding`, `done`, `failed`, `pruned` (files removed by `timelapse.retention`). `video` is true once
`job/timelapse` has the file. `layers` is in capture order:

- The snapshot taken when `job.layer` changes to n shows layer n − 1 finished and is that layer's
  frame. The last layer has none: the job ends after the end G-code, which on the CHX 350 has
  lowered the bed by then (2026-09-28). Show the latest earlier frame for it.
- `frame` null: skipped (`reason: "interval"`, `timelapse.minIntervalS`) or failed (`reason: "snapshot: …"`).
  Show the latest earlier frame then. A layer taken twice (daemon restart) counts with its last frame.
- Frame numbers ascend without gaps; frame n of the video is at `(n + 0.5) / fps` for a `<video>`.
- M240 frames: `waitMs` from the M240 to the snapshot, `still` whether the picture had stood still (null:
  not judged). From the second M240 frame of a job on, against the M240 frame before it: `changedPx`
  pixels of the grey 160×90 thumbnails differ by more than 24 levels, `changedBox` where
  (`[left, top, right, bottom]` as fractions of the picture, null when none), `afterPause: true` when
  the job paused between the two. Both photos show the head parked, so a change beyond the new layer
  means something moved (a part that came loose, spaghetti) — provided the park macro puts the head
  in the same place every time. Frames taken at layer changes are not compared.

`job/timelapse/frame` answers from the captured JPEG while it exists (during the print, until the
video is verified, or kept after a failed encoding), else extracts the frame from the video
(dav1d, keyframe every `keyframeInterval` frames, well under a second). Load the video once as a
Blob and seek locally rather than asking for frame after frame: DSF sends files without Range
support, and each extraction costs the SBC CPU.

Encoding runs after the job, one at a time and never while a job prints (a job that starts pauses
it). `status.timelapse`: `{enabled, reason, capturing: [jobId], encoder: {state: idle|encoding|paused,
jobId, queued}, lastError}`.

## Object model journal

Every change of the object model during a job (`dsf/qa_journal.py`, Tim 2026-09-29), so that problems nobody
thought of can still be looked into. The file (`job/om/journal`) is gzip, one JSON object per line:
`{"t": <epoch ms>, "snapshot": <the whole model>}` or `{"t": <epoch ms>, "patch": <DSF's patch>}`, both as DSF sent
them (no float rounded). Snapshots come at the job start, every `journal.snapshotIntervalMin` (10) and at the job
end. A patch follows DSF's rules: an object carries what changed, a list its new length with `{}` for an unchanged
object, `messages` only the new messages. To rebuild the model at a time, start from the last snapshot before it
and merge the patches up to it (`qa_journal.merge`; `job/om` does that). About 5 MB per printing hour on the
CHX 350.

```python
import gzip, json
for line in gzip.open("qa-<job>-om.jsonl.gz", "rt"):
    entry = json.loads(line)   # {"t": ..., "snapshot": {...}} or {"t": ..., "patch": {...}}
```

## Trends

`metric`: `heater_load_mean` (per nozzle heater and setpoint, with `nozzleDiameter`), `fm_avg_percentage`,
`filament_ratio`, `mm_per_rev` (per monitor), `esteps_suggested`, `heat_up_s` (per heater), `duration_s`,
`events` (count without start, end and calibrations, `byType`), `spectrum_peak_hz` and `spectrum_rms` (per `axis`: mean
of the job's recordings, from `summary.mechanics`, with `spectra`), `fan_rpm` and `fan_amplitude` (per `fan` with its
`name`: `rpmMean` and `amplitudeMean` from `summary.fans`, with `pwm`, `stalled`, `amplitudeMax`, `recordings`). Each
point carries `jobId`, `ts`, `result`, `material`, `value`.

## Spectra

Every `accelerometer.intervalMin` minutes while a job prints (status `processing`, from layer 2 on)
QA records `accelerometer.samples` samples with M956 on the SBC channel from the accelerometer on
the board `accelerometer.board` (CAN address; `null`, the default, means no recordings) and stores
one spectrum per axis. Spectrum: `{id, ts_ms, layer, board, axis, sampling_rate, n_samples, freqs, amplitudes, peak_hz,
rms, source, job_key}` — `freqs` in Hz (k × rate/N up to Nyquist), `amplitudes` in g exactly as DWC's
input-shaping plugin computes them (@duet3d/motionanalysis `analyzeAccelerometerData`, wide band,
Hann window), `peak_hz` the largest amplitude from 5 Hz on, `rms` in g without the mean (gravity),
`sampling_rate` the rate RRF measured, `source` the accelerometer's port (`60.i2c.lis`), `fans` the fans below.

A fan's imbalance puts a line into every spectrum at its rotation frequency (on the CHX 350 the part-cooling
blowers made the largest peak at 152-155 Hz, 2026-09-30). `fans`: the fans with a tachometer (`rpm` ≥ 0) that
turned or were driven while the recording ran, `[{fan, name, pwm, rpm, hz, amplitude, sharedWith}]` — `fan` the
index in `fans[]`, `pwm` the mean `actualValue` (0..1, null when RRF reports −1), `rpm` the mean over the model
updates from M956 until the run finished, `hz` = rpm / 60 (null while it stands), `amplitude` this axis' largest
amplitude within 1.5 bins of `hz` (null for a standing fan or a line above Nyquist), `sharedWith` the fans whose
lines lie within 3 bins, so that the amplitude is one peak for all of them. `pwm` > 0 with `rpm` 0: the fan stood
although driven. The line is right only if RRF's `tachoPpr` matches the fan. `null`: not recorded (spectra of QA
before schema 4, or no fan with a tachometer); `[]`: no fan turned.

Reference per axis: `{axis, mode, setAt, spectrumIds, jobIds, complete, freqs, amplitudes, peakHz, rms}`.
`auto`: element-wise median of the first `referenceAutoCount` spectra of the axis, on the frequency grid
of the first (the others interpolated); `complete` false while there are fewer. `manual`: one chosen
spectrum. The job summary's `mechanics` holds per axis `{spectra, peakHzMean, peakHzMax, rmsMean}`, its `fans`
per fan `{name, recordings, pwmMean, rpmMean, stalled, amplitudeMean, amplitudeMax}` (the amplitude as the root
sum square over the recorded axes; `stalled` = recordings in which it stood although driven).

`status.accelerometer`: `{enabled, reason, accelerometer: {index, port, board, samplingRate, resolution},
available: [{index, port, board}], intervalMin, pending, lastRecording, lastError}`; `index` is the
M955/M956 P number, `available` lists every accelerometer configured with M955, `accelerometer` the
one `accelerometer.board` selects.

## live (WebSocket)

`ws(s)://<host>/machine/QualityAssurance/live?sessionKey=<key>`. The client sends one text frame
after connecting (any text; DSF passes the session along with it). Without a session the socket is
closed with 1008, also when no frame arrives within 10 s. Frames (JSON text):

| type | |
|---|---|
| `hello` | after the client's first frame: `version`, `status` |
| `sample` | at most once a second while a job records: `values` (all channels plus `heater.<n>.load` of nozzle heaters), `heaterLoad` per heater `{mean, level}` |
| `event` | every event as stored |
| `layer` | a layer started (`layer`) or finished (`finished`, `durationS`) |
| `job` | `event: "end"`, `jobId`, `result` |
| `status` | while idle, every 25 s |
| `ping` | when nothing else was sent for 25 s (the HMI's haproxy keeps WebSocket tunnels 1 h) |

Messages after the first are ignored. A client that falls 500 frames behind loses the oldest.
