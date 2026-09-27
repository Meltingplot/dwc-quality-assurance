# Quality Assurance — Developer Guide (DWC/DSF 3.7)

Plugin `QualityAssurance` records process parameters of every print job on the CHX 350 and
serves them over HTTP. It **records only**; judging, findings and reports belong to the later
Quality Control plugin. The design and every decision behind it: [PLAN.md](PLAN.md).
Neighbours whose conventions apply: `../dwc-vigil` (machine counters, its `CLAUDE.md` holds the
DWC/DSF lessons), `../DuetWebControl/src/plugins/CHX350` (operator UI + SBC backend).

## ⚠️ Rule 1: never guess an interface
Read the code or the wiki before writing against dsf-python, DSF, DWC or RRF:

| What | Where |
|---|---|
| dsf-python 3.7.0b1 (what the image runs) | `pip download dsf-python==3.7.0b1`, or git `Duet3D/dsf-python@3.7.0-beta.1` |
| DSF | `../DuetSoftwareFramework` (Meltingplot fork, v3.7-dev) |
| DWC | `../DuetWebControl` (Meltingplot fork, v3.7-dev; builder in `scripts/build-plugin*.js`) |
| RRF behaviour | Duet3D wiki `../wiki-content` first, RRF source (`Duet3D/RepRapFirmware@3.7-dev`, `Duet3D/CANlib`) second |

## ⚠️ Rule 2: every fact carries its source and date
A comment or note that states how DSF/DWC/RRF/dsf-python behaves names what it was verified
against and when, e.g. `(DuetSoftwareFramework v3.7-dev @ cd3ae65f, 2026-09-26)`. Upstream
`-dev` branches move; the date tells the next reader whether to re-check. QA is 3.7-only, so
no generation tags.

## Layout
```
plugin.json                 manifest (data keys = PLUGIN_DATA_KEYS in dsf/qa-daemon.py)
src/                        DWC 3.7 frontend, TypeScript + Vue 3.5 + Vuetify 4
  index.ts                  entry: registerPluginMessages, registerRoute, ensureBackendRunning
  host.ts                   HostAdapter on DWC's Pinia machine store — the only DWC seam
  core/                     framework-neutral (no store imports): backend, api, charts, …
  components/, i18n/
dsf/                        Python daemon (copied verbatim into the package, minus tests)
  qa-daemon.py              entry point: subscription loop, heartbeat, retention, plugin data
  qa_patches.py             dsf-python patches (§ Object model)
  qa_log.py                 errors → stderr, warnings → write_message
  qa_api.py                 HTTP endpoint registry, ApiContext, LiveHub (WebSocket fan-out)
  qa_settings.py            settings.json with defaults and validation
  qa_db.py                  SQLite: schema, writer thread, quarantine/restore, backup, retention
  qa_collector.py           jobs, ring buffer, blocks, events, layers (the main-thread logic)
  qa_channels.py            channel extraction, heater roles, positions
  qa_heaterload.py          heater load, same definition as the CHX UI banner
  qa_machine.py             MFM globals of chx350-config, context globals
  qa_context.py             job context snapshot, CRC32
  qa_summary.py             layer aggregates, job summary (time-weighted)
  qa_slicer.py              CONFIG_BLOCK parser (copy from the CHX350 backend)
  qa_gcode.py               layer index (as job.layer counts) and toolpath
  qa_timelapse.py           snapshot per layer, AV1 encoding after the job (ffmpeg), frames
  qa_accel.py               M956 recordings, CSV, spectra (pure-Python FFT), references
tests/                      pytest (test_*.py, real dsf-python) + vitest (*.test.js)
scripts/                    ci-local.sh, verify-package.sh, version.js
docs/                       image.md (work order for the image build), …
```

## Object model access (dsf-python 3.7.0b1)
Typed model: `get_object_model()` once, then per patch `json.loads(get_object_model_patch())`
→ `model.update_from_json(patch)`. Read with `getattr()` and snake_case names.
`dsf/qa_patches.py` fixes what the library gets wrong; `tests/test_patches.py` runs it against
the real library. Verified 2026-09-26 against dsf-python 3.7.0b1 and DSF v3.7-dev @ cd3ae65f:

| Problem | Patch |
|---|---|
| Greeting read with `recv(50)`, DSF 3.7 sends more | full JSON read in `_connect` (from Vigil/CHX350) |
| `"customInfo": null` → TypeError drops the whole patch | `_set_model_prop(None)` clears dict/collection (from Vigil) |
| Unknown enum values (`timedOut`, `motorStallEncoder`) abort the update | `_missing_` hook + safe `Board.state`/`Axis.letter` setters (from Vigil) |
| `camel_to_snake("k0") == "k_0"`: `pressAdv.k0/k1`, `build.m486Names/m486Numbers` never written (stay default) | alias property under the split name |
| `RotatingMagnetFilamentMonitor.agc`, `calibrated.mmPerRev`, `FilamentMonitor.filamentPresent` missing | added as `model_prop`s |
| `BuildObject.cancelled` spelled `canceled` | alias |
| `sensors.accelerometers` missing (DuetAPI moved it from `Board`, 524fdc4c), no `port`/`resolution`/`samplingRate` | collection of the library's `Accelerometer` + the three props (verified 2026-09-27) |

`messages[]` is read from the raw patch dict, not from the typed model (the typed collection
is overwritten index by index by every patch). DSF sends only new messages in a patch
(`ModelSubscription.cs`, MessageCollection).

## DSF facts QA relies on (DuetSoftwareFramework v3.7-dev @ cd3ae65f, 2026-09-26)
- A job runs while `job.duration is not None`; `job.file.fileName` keeps the last name after a
  job on DSF 3.7. The outcome flags `lastFileCancelled/Aborted` arrive **after** the job-end
  patch: wait for them to change, or 10 s (dwc-vigil `VigilTracker`, same DSF source).
- dsf-python serves each endpoint in its own thread + asyncio loop; `read_request` reads
  ≤ 32 KiB. The shared `CommandConnection` is used only under `ApiContext.cmd_lock`.
- ⚠️ DSF does **not** authorize plugin endpoints (`/machine/*` routes require a session, plugin
  requests reach `CustomEndpointMiddleware` only when no route matched). It looks the session up
  (`X-Session-Key`; WebSocket: `?sessionKey=`) and passes `request.session_id`, −1 = anonymous.
  QA refuses −1 everywhere (`qa_api.call`, `make_live_handler`): 401, WebSocket close 1008.
  Frontend requests only through the connector (`host.request`), files as Blobs.
- Custom WebSocket endpoints: DSF opens one Unix-socket connection per browser client and
  forwards every `send_response` as a text frame (status ≥ 1000 closes with that code); nothing
  is sent on connect, the session comes with each client text frame (`CustomEndpointMiddleware.cs`).
- `HttpResponseType.File`: DSF streams the file as `application/octet-stream`, no Range support.
- DSF keeps a crashed daemon's endpoints registered: "running" is `plugins.QualityAssurance.pid > 0`.
- Manifest: `sbcAutoRestart` restarts 2 s after an unexpected exit; permissions used:
  commandExecution, objectModelReadWrite, registerHttpEndpoints, fileSystemAccess, readGCodes,
  networkAccess (camera), launchProcesses (ffmpeg), readSystem/writeSystem (accelerometer CSVs
  in `0:/sys/accelerometer`). `sbcPackageDependencies: ["ffmpeg"]`, no Python packages beyond dsf-python.
- Accelerometers are `sensors.accelerometers[]`, the index is the M955/M956 P number; `runs` counts
  every finished run (`points` 0 = failed). M956 on the SBC channel starts at once without a
  movement lock (RRF 3.7-dev @ 3638836 `Accelerometers.cpp`); in SBC mode `runs` advances before the
  CSV's last line is written.

## Recording decisions (details in the module docstrings)
- Timestamps are epoch ms (wall clock); jobs have a text id for the API and an integer key for
  the sample tables. Samples: coarse every `sampleIntervalS`; fine rows only for changed values
  inside a block (ring buffer + `postTriggerS`).
- Layers follow `job.layer`. The G-code index counts layers the way DSF forwards comments and RRF
  parses them (`qa_gcode.py`), so index and `job.layer` agree. A layer's `z` is the Z of its last
  extruding sample; `height`/`fractionPrinted` come from `job.layers[]` when DSF has the layer.
- Filament monitor `configured` is a setpoint (event on change); `calibrated` only as appearing /
  disappearing, its values are channels (they change continuously).
- Retention keeps a job's raw data while it is one of the newest `retention.jobs` **or** younger
  than `retention.days`, then the size cap removes the oldest.
- Phantom reading: a jump ≥ `phantomJumpK` within one patch that returns to within
  `temperatureK` of the value before inside `phantomReturnS`.
- Timelapse: the snapshot at the change to layer n is layer n − 1's frame (the last one at the job
  end); frames numbered without gaps; encoding only while no job prints (SIGSTOP/SIGCONT), with
  nice 19 + I/O idle set on the encoder thread and inherited by ffmpeg; verified video (packets =
  frames) before the JPEGs go. Tested against trixie's ffmpeg 7.1.5 in a container
  (`test_real_ffmpeg` runs where ffmpeg has libsvtav1).
- Accelerometer: every `intervalMin` while `processing`, from layer 2 on, one recording at a time;
  spectra exactly as DWC's input-shaping plugin (@duet3d/motionanalysis, checked number for number in
  `test_spectrum_equals_dwc_motionanalysis`); no retries, one `accelerometer_failed` event.
- Driver errors: new bits of CANlib's `StandardDriverStatus` ErrorMask/WarningMask/stall on
  boards that report status; RRF's event text in `messages[]` for the others (only printed when
  no `driver-*.g` handler exists — RRF `GCodes::ProcessEvent`).

## Build, test, release
```bash
npm ci && npm run lint && npm test              # vitest, real Vuetify 4, [Vue warn] = failure
scripts/ci-local.sh python                      # pytest in .ci-local/venv with dsf-python 3.7.0b1
scripts/ci-local.sh build                       # package against Meltingplot/DuetWebControl v3.7-dev
```
- DWC's builder type-checks every `*.ts` in the plugin dir against DWC's sources, which is
  why tests are `.js`. It externalises `@/stores/*`, `@/plugins`, `@/i18n`, `@/utils/*`, `vue`,
  `vuetify`, `pinia`; `chart.js` is bundled from our `node_modules`.
- The builder writes `dist/`, `pkg/`, `QualityAssurance-<ver>.zip` and `-srcmap.zip` into the repo;
  address the package by exact name.
- Releases: push to `release` (or dispatch). Assets: ZIP + `.sha256`; the release notes carry the
  `plugins.list` line for the image build (docs/image.md). Pre-releases only from `main`.
- PR track: never commit to `main` directly.

## Checklist for a change
1. Interface read, not guessed (Rule 1); facts dated (Rule 2).
2. New `set_plugin_data` key → `plugin.json#data` and `PLUGIN_DATA_KEYS`.
3. New dsf-python patch → `qa_patches.py` + a case in `tests/test_patches.py` (real library).
4. Persistent data only under `/opt/dsf/sd/QualityAssurance/`. A new endpoint goes into
   `ENDPOINTS` (so `qa_api.call` refuses requests without a session), never around it.
5. Components mount against real Vuetify 4 without warnings; i18n keys in en **and** de.
6. `npm run lint`, `npm test`, `pytest` green; `ci-local.sh build` when build or manifest changed.
