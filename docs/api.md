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
  `useMachineStore().request(...)`, not `fetch()`. Files (export, later the timelapse) are
  requested with `responseType: "blob"`; a bare `<a href>`/`<img src>`/`<video src>` has no key.
- WebSocket: `?sessionKey=<key>` in the URL, then one text frame from the client (DSF hands the
  daemon the session only with client frames). `hello` comes only after that.
- A server-side caller (a later Quality Control daemon) needs a session too: `GET
  /machine/connect?password=…` returns `sessionKey`.
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
| GET | `job` | `id` | JobEntry + `context`, `summary`, `fileCrc32`, `startLayer`, `durationS`, `warmupS`, `pauseS` |
| GET | `job/layers` | `id` | `{jobId, meta, layers: [Layer]}` |
| GET | `job/events` | `id`, `type` (comma list) | `{jobId, events: [Event]}` |
| GET | `job/samples` | `id`, `channels` (comma list, ≤ 50), `from`, `to` (epoch ms), `resolution` (`coarse`, `fine`, `auto`) | `{jobId, from, to, resolution, downsampled, channels: {name: [[ts_ms, value]]}}` |
| GET | `job/blocks` | `id` | `{jobId, blocks: [{id, start_ms, end_ms, triggers}]}` |
| GET | `job/spectra` | `id` | `{jobId, spectra: []}` (accelerometer phase postponed) |
| GET | `job/toolpath` | `id`, `layer` | segments of the layer; 202 while the index is built; 409 when the file is gone or changed |
| GET | `job/export` | `id` | the job as one JSON file (`application/octet-stream`) |
| GET | `trends` | `metric`, `limit` (100), `material` | `{metric, points: [...]}` newest first |
| GET | `channels` | – | `{channels: [...], derived: [...]}` |
| GET/POST | `spectra/reference` | POST body `{axis, spectrumId}` or `{axis, mode: "auto"}` | `{references: [...]}` |
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
`heaterLoad` thresholds `{high, limit}`.

Each layer (`job.layer` numbering, i.e. RRF's):

| Field | |
|---|---|
| `layer`, `startedAt`, `endedAt`, `durationS` | |
| `height` | layer thickness from DSF's `job.layers[]` when available, else null |
| `z` | machine Z of the last extruding sample of the layer (cumulative height) |
| `fractionPrinted` | |
| `filament` | per extruder/monitor `{commandedMm, measuredMm, extruderMm, ratio}`: commanded = Δ monitor `totalExtrusion`, measured = Δ `calibrated.totalDistance`, extruder = Δ `move.extruders[].position` |
| `flow` | per extruder, mm³/s: commanded filament × cross-section / layer duration |
| `temps` | `heaters` per heater index `{min, max, mean, std, setpoint}`, `sensors` per analog sensor index `{…, name}` (not compact, unlike `job.layers[].temperatures`), `chamber` `{min, max, mean, std}` |
| `fmStats` | per monitor: `lastPercentage` `{min, max, mean, std}`, `avgPercentage`, `mmPerRev` at layer end |
| `pwmStats` | per heater at a constant, reached setpoint: `avgPwmMean`, `avgPwmStd`, `currentStd` |
| `loadStats` | per heater: `mean`, `max`, `p95` (load at the setpoint), `maxMean60`, `atSetpointS`, `shareAtSetpoint`, `shareHigh`, `shareLimit` (time share of the 60 s mean ≥ high/limit), `setpoint` |

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
| `heater_fault` | – | `heater`, `previousState`, `current` |
| `heater_monitor` | `tooHigh` / `tooLow` | `heater`, `monitor`, `limit`, `reading`, `sensor`, `action` |
| `heater_load` | `high` / `limit` | `heater`, `tool`, `setpoint`, `level`, `peakMean`, `levels`, `volumetricFlow`, `speedFactor` |
| `mfm_error_tolerated` | – | `count` + the MFM globals |
| `mfm_recovery` | `requested` / `false_positive` / `real_issue` | `result` + the MFM globals |
| `mfm_flow_bias` | `detected` | the MFM globals (`mfm_esteps_suggested`, …) |
| `setpoint_change` | `heater.active`, `heater.standby`, `heater.maxPwm`, `stepsPerMm`, `pressAdv.k0/k1/d`, `nonlinear.a/b/upperLimit`, `fm.configured`, `fm.calibrated` | `setpoint`, `index`, `from`, `to`; `stepsPerMm` also `cause` (`mfm_flow_bias`, `baseline_restore`, null) |
| `babystep` | – | `axis`, `from`, `to` |
| `driver_error` | `error` / `warning` / `stall` | `source` `status`: `board`, `canAddress`, `driver`, `status`, `previous`, `bits`; `source` `message`: `canAddress`, `driver`, `text` |
| `voltage_dip` | – | `board`, `vIn`, `median90s`; end: `recoveredTo` |
| `phantom_reading` | `heater` / `sensor` | `channel`, `before`, `peak`, `durationMs` |
| `timelapse_failed`, `accelerometer_failed` | | later phases |

## Channels

| Channel | |
|---|---|
| `heater.<n>.current`, `.active`, `.standby`, `.avgPwm`, `.state` | state: 0 off, 1 standby, 2 active, 3 fault, 4 tuning, 5 offline |
| `heater.<n>.load` | derived at read time from `avgPwm` and the `maxPwm` valid then (not stored) |
| `extruder.<n>.position`, `.rawPosition`, `.factor`; `job.rawExtrusion` | |
| `fm.<n>.status`, `.lastPercentage`, `.avgPercentage`, `.minPercentage`, `.maxPercentage`, `.position`, `.totalExtrusion`, `.agc`, `.calibrated.totalDistance`, `.calibrated.mmPerRev` | status: 0 ok, 1 noMonitor, 2 noDataReceived, 3 noFilament, 4 tooLittleMovement, 5 tooMuchMovement, 6 sensorError |
| `sensor.<n>.lastReading` | every analog sensor |
| `axis.<L>.machinePosition` | |
| `move.currentMove.topSpeed`, `.requestedSpeed`, `.extrusionRate`; `move.speedFactor` | |
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

## Trends

`metric`: `heater_load_mean` (per nozzle heater and setpoint, with `nozzleDiameter`), `fm_avg_percentage`,
`filament_ratio`, `mm_per_rev` (per monitor), `esteps_suggested`, `heat_up_s` (per heater), `duration_s`,
`events` (count without start/end, `byType`). Each point carries `jobId`, `ts`, `result`, `material`, `value`.

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
