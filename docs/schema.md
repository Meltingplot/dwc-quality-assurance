# Storage

Everything QA keeps lives under `/opt/dsf/sd/QualityAssurance/` (the SBC's persistent volume; the
plugin directory is replaced on every upgrade). Read it through the HTTP API ([api.md](api.md)); the
database layout is QA's own and may change with `SCHEMA_VERSION`. A later Quality Control plugin
uses the API, not the database (PLAN.md §8).

## Files

| Path | What |
|---|---|
| `qa.db`, `qa.db-wal`, `qa.db-shm` | the SQLite database (WAL mode) |
| `qa.backup.1.db`, `qa.backup.2.db` | backups after every job, newest first (`integrity_check`, then `VACUUM INTO`) |
| `qa.db.corrupt.<epoch>` | a database that failed `quick_check` at start-up, moved aside (the newest good backup replaced it) |
| `settings.json` | settings (defaults in `dsf/qa_settings.py`, validated on load and on `POST settings`) |
| `index/<crc32>.json` | G-code layer index per job file (byte ranges and modal state per layer), rebuilt when missing |
| `timelapse/<job id>/frames/NNNNNN.jpg` | snapshots while a job prints and until its video is verified (kept after a failed encoding with `keepFramesOnFailure`) |
| `timelapse/<job id>/timelapse.mp4` | the AV1 video; `encode.log` beside it only after a failed encoding |
| `journal/<job id>/journal.jsonl.gz` | the object model journal: DSF's patches and snapshots, one gzip member per minute, each snapshot a member of its own (api.md "Object model journal") |
| `journal/<job id>/index.jsonl` | its members: `{start, end, offset, length, snapshot}`, written after the member; a daemon start cuts the data file back to the last indexed member |
| `tmp/export-<job>.json`, `tmp/frame-<job>-<n>.jpg` | files DSF sends for `job/export` and `job/timelapse/frame`; removed after an hour |

The accelerometer CSVs are RRF's: `0:/sys/accelerometer/qa-<job id>-<epoch s>.csv`, deleted once read.

## Durability

`journal_mode=WAL`, `synchronous=FULL`. One writer thread owns the only write connection: samples
are committed every `commitIntervalS` (30 s, the loss window on power failure), events, job records,
timelapse state and spectra at once. Endpoint threads read through their own read-only connections.
Start-up runs `quick_check`; a damaged file is replaced by the newest backup that passes it, else by
a new database.

## Retention

| What | Kept |
|---|---|
| samples, blocks (raw data) | while the job is one of the newest `retention.jobs` (50) **or** younger than `retention.days` (90); then, while the database is larger than `retention.maxDbBytes` (2 GB), the oldest go. `jobs.raw_pruned` = 1 afterwards |
| jobs, job_layers, events, spectra, reference_spectra | always |
| timelapse files | the newest `timelapse.retention.jobs` (50), then oldest first while all of them exceed `timelapse.retention.maxBytes` (10 GB); the `timelapse` row stays with status `pruned` |
| object model journal | as long as the job has its raw data (`jobs.raw_pruned` = 0) and the running job's always; then oldest first while all of them exceed `journal.maxBytes` (2 GB). Not in the backups: in the database, each backup after a job would copy it |
| layer index cache | 8 in memory; the files are not cleaned up (one per distinct job file, typically well under 1 MB) |

## Tables

Times are epoch milliseconds (`*_ms`, `started_at`, `ended_at`, `set_at`). JSON columns hold compact
JSON text. Jobs have a text `id` (`YYYYMMDD-HHMMSS-<crc>`, what the API uses) and an integer `key`
that the large tables reference.

### `schema_meta`
`key`, `value`: `version` (the schema version, `SCHEMA_VERSION` in `dsf/qa_db.py`).

### `jobs`
| Column | |
|---|---|
| `key` | integer primary key |
| `id` | text id, unique |
| `file_name` | virtual path (`0:/gcodes/…`) |
| `file_crc32` | CRC32 of the file at job start (8 hex digits) |
| `started_at`, `ended_at` | |
| `result` | `running`, `completed`, `cancelled`, `aborted`, `unknown` (the daemon was not running when the job ended) |
| `partial`, `start_layer` | the daemon started during the job: recording began at `start_layer` |
| `num_layers` | from the file info |
| `duration_s`, `warmup_s`, `pause_s` | RRF's job times |
| `material` | slicer filament profile, else the loaded filament |
| `context` | JSON, see "Job context" |
| `summary` | JSON, see "Job summary"; null while running |
| `raw_pruned` | samples and blocks removed by retention |

### `job_layers`
Primary key (`job_key`, `layer`); `layer` counts like `job.layer`. `started_at`, `ended_at`,
`duration_s`, `height`, `z`, `fraction_printed` and the JSON columns `filament`, `flow`, `temps`,
`fm_stats`, `pwm_stats`, `load_stats`, `filament_path` (schema version 2), `feed` (schema version 3) — the fields of api.md "Layer"
(snake_case here).

### `channels`
`id`, `name` (e.g. `heater.1.current`; the names are listed in api.md "Channels").

### `samples`
Primary key (`job_key`, `channel`, `ts_ms`), without rowid. `value`; `resolution` 0 = coarse (every
`sampleIntervalS`, every channel), 1 = fine (only values that changed, inside a block); `block_id`
of the fine block a row belongs to. A coarse and a fine sample at the same time share one row on
the coarse grid that carries the block id.

### `blocks`
Full-resolution windows around triggers: `id`, `job_key`, `start_ms`, `end_ms`, `triggers` (JSON
`[{ts, reason}]`). A block starts with the oldest ring-buffer entry, up to `ringBufferS` before its
first trigger, and ends `postTriggerS` after the last.

### `events`
`id`, `job_key`, `ts_ms`, `end_ms` (ongoing conditions), `type`, `subtype`, `layer`, `x`, `y`, `z`
(machine position), `positions` (JSON, every axis), `workplace`, `offsets` (JSON, that workplace's
offsets per axis), `tool`, `object_id`, `device`, `payload` (JSON), `block_id`. Types and payloads:
api.md "Event".

### `timelapse`
One row per job with a timelapse: `job_key` (primary key), `status` (`capturing`, `queued`,
`encoding`, `done`, `failed`, `pruned`), `codec` (`av1`), `path`, `frames`, `fps`, `size_bytes`,
`layer_frames` (JSON `[{layer, frame, ts, reason?}]` in capture order, api.md "Timelapse"), `error`.

### `spectra`
One row per axis of an accelerometer recording: `id`, `job_key`, `ts_ms`, `layer`, `board` (CAN
address), `axis`, `sampling_rate` (measured, Hz), `n_samples`, `freqs`, `amplitudes` (JSON arrays,
Hz and g), `peak_hz`, `rms` (g), `source` (the accelerometer's port). api.md "Spectra".

### `reference_spectra`
`axis` (primary key), `mode` (`auto` or `manual`), `spectrum_id` (manual), `set_at`. The automatic
reference is computed when asked for (median of the first `accelerometer.referenceAutoCount`).

## Job context (`jobs.context`)

A snapshot at job start of everything that describes how the job was made, so it stays readable
when the machine or the file changes later.

| Key | |
|---|---|
| `file` | `job.file` of the object model (`fileName`, `generatedBy`, `layerHeight`, `numLayers`, `height`, `printTime`, `simulatedTime`, `filament`, `size`, `lastModified`, `customInfo`) and `crc32` |
| `slicer` | `{source, config}`: selected keys of the OrcaSlicer/BambuStudio `CONFIG_BLOCK` at the end of the file (`filament_settings_id`, `filament_type`, `nozzle_diameter`, …), `source` `none` without one |
| `extruders` | per extruder `index`, `stepsPerMm`, `factor`, `filament`, `filamentDiameter`, `microstepping` `{value, interpolated}`, `pressAdv` `{k0, k1, d}`, `nonlinear` |
| `tools` | `number`, `name`, `heaters`, `extruders`, `filamentExtruder`, `active`, `standby`, `offsets` (axis order), `retraction` (M207: `length`, `extraRestart` mm, `speed`, `unretractSpeed` mm/s, `zHop` mm) |
| `filamentMonitors` | `index`, `type`, `enableMode`, `configured`, `calibrated` |
| `heaters` | `index`, `role` (nozzle, bed, chamber, other), `tool`, `sensor`, `sensorName`, `max`, `model` (`maxPwm`, `heatingRate`, … `pid`), `monitors` |
| `sensors` | analog sensors `{index, name, type}` |
| `chamber` | `["sensor" \| "heater", index]` by the chamber rule, or null |
| `shaping` | input shaper (M593): `type`, `frequency`, `damping`, `amplitudes`, `delays` (until 2026-09-29 `durations`, which RRF 3.7 does not report) |
| `axes` | `letter`, `stepsPerMm`, `microstepping` `{value, interpolated}`, `babystep` |
| `boards` | `index`, `name`, `shortName`, `canAddress`, `firmwareVersion`, `firmwareDate` |
| `accelerometers` | `sensors.accelerometers`: `index` (M955/M956 P), `port`, `board`, `orientation`, `samplingRate`, `resolution` |
| `versions` | `firmware`, `dsf`, `plugin` |
| `globalsStart` | the `contextGlobals` at job start (missing globals are left out) |
| `globalsEnd` | the same at job end (added then) |
| `macros` | every macro the file calls (`M98 P` argument), read while the job runs once the layer index is ready: `crc32`, `pathMm`, `netMm`, `fwRetracts` (its own E travel, as `qa_gcode.macro_stats` counts it), `approximate`, `calls` (nested M98), `crc32End` when it differed at the job end; null when unreadable |
| `feedReference` | per extruder the e-steps it first fed filament with inside a layer (after the start G-code and a tool's filament config): `{stepsPerMm, layer}`, added then; the base of the layers' `feed` |
| `gearForecast` | every layer's gear passes in advance, computed once at the first layer start with the layer index ready: `threshold`, `atLayer`, `retraction` (M207 then), `layers` (layer → gearPasses, as `filamentPath.gearPasses`), `runs` `[{firstLayer, lastLayer, max, maxLayer}]` at the threshold or more, `unknownMacros`; see the event `gear_passes_forecast` |
| `material` | as the column |

## Job summary (`jobs.summary`)

Written at job end.

| Key | |
|---|---|
| `result`, `abortReason` | as the job; `abortReason` for cancelled/aborted jobs: an event type, `message: <title>` or `user` |
| `layers` | number of layers recorded |
| `filament` | per monitor: `commandedMm`, `measuredMm`, `extruderMm`, `ratio` (measured/commanded), `avgPercentage` and `mmPerRev` at the end, `percentDistribution` `[{from, to, s}]` (2 % classes, time), `flowVsPercent` `[{flowFrom, flowTo, mean, std, s}]` |
| `thermal` | `chamber` `{min, max, mean, std}`, `avgPwmAtSetpoint` per heater, `heatUp` per heater `[{setpoint, s}]`, `heaterLoad` per heater `{mean, max, p95, maxMean60, atSetpointS, shareHigh, shareLimit, setpoint, bySetpoint: [{setpoint, mean, timeS}], nozzle, tool, events, firstEventLayer}` |
| `mfm` | the machine's filament-monitor evaluation: `toleratedErrors`, `recoveries`, `flowBiasDetected`, `estepsSuggested`, `estepsApplied` |
| `events` | per type `{count, firstLayer, lastLayer}` |
| `spoolUsageG` | per tool, from the `spool_remaining` global at start and end |
| `mechanics` | per axis `{spectra, peakHzMean, peakHzMax, rmsMean}` of the accelerometer recordings, null without |
