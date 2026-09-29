# CHX 350 UI: history and analysis from QA

Work order for the DuetWebControl fork (`Meltingplot/DuetWebControl`, `src/plugins/CHX350`). QA does
not change the fork; it provides the API (docs/api.md) and two embeddable views (§4), and pins the
contract in `tests/test_api.py` (`test_jobs_contract_for_the_chx_ui`,
`test_layers_contract_for_the_chx_analysis`) and `tests/components/embed.test.js`.
Read against the fork's v3.7-dev @ 1fb6a51 on 2026-09-26 (PLAN.md §5.10); §1–3 are implemented
there since ecd9b76 (2026-09-28).

## 1. QA is running

Same rule as the CHX350 backend (`api.ts` `backendAvailable`): the pid in the object model, not
a probe request, because DSF keeps a crashed daemon's endpoints registered.

```ts
export const qaAvailable = computed(() => (useMachineStore().model.plugins.get("QualityAssurance")?.pid ?? -1) > 0);
```

Requests like the existing `get<T>()` in `api.ts`: `useMachineStore().request("GET", "machine/QualityAssurance/<path>", params, "json", null, 15000, …)`.
Only through the connector: QA answers 401 to a request without a DWC session (docs/api.md
"Authentication"), so no `fetch()` and no bare `<img>`/`<video src>`; files as `responseType: "blob"`.

## 2. History (`composables/useJobHistory.ts`, "the seam")

Source order in `load()`: **QA `jobs`** (`qaAvailable`) → CHX350 backend `history` → event log in the
browser. Request: `GET machine/QualityAssurance/jobs?limit=100`. Each entry already has the
`HistoryEntry` fields plus `analysable`:

| QA field | `HistoryEntry` / `JobHistoryItem` |
|---|---|
| `file` | `file` (virtual path) |
| `result` | `result`: `finished`, `cancelled`, `aborted`, `running` — **and `unknown`** (QA's daemon did not run when the job ended). Extend the union and give it a label, or show it like `aborted` without a reason. |
| `printTimeS` | `printTimeS` |
| `timestamp` | `timestamp` (ISO UTC, end time; start time while running) |
| `analysable` | `true` for every QA entry |
| `id` | new: needed to open the analysis of an older job |

The running job: QA lists it with `result: "running"`, so `current` from the object model and the
first QA entry describe the same job. Merge by `file` as today (or by QA `id` once `current` carries it).

With QA the entries become "Analyse öffnen" instead of "Analyse folgt" (`analysable: true`).

## 3. Analysis (`composables/useJobAnalysis.ts`)

Today it reads `job.layers[]` of the running/last job. For an entry from QA, read
`GET machine/QualityAssurance/job/layers?id=<id>` and build the same `LayerChannel`s:

| CHX channel | from QA `layers[]` |
|---|---|
| `layers` (for `heights`) | `height` per layer (layer thickness; null when DSF had none). `z` is the cumulative Z directly |
| `cumulativeFilament` | running sum of `filament["0"].extruderMm` (or `commandedMm`) |
| `duration` | `durationS` |
| `filament` | `filament["0"].extruderMm` per layer |
| `flow` | `flow["0"]` (mm³/s, already computed with the extruder's filament diameter) |
| `sensor<i>` | `temps.sensors["<i>"].mean`, label `temps.sensors["<i>"].name` — by sensor **index**, not compact like `job.layers[].temperatures`, so `temperatureColumn()` is not needed |
| `chamberChannel` | `meta.chamber` is `["sensor", i]` or `["heater", i]` (QA's chamber rule: chamber heater, else sensor named "SZP coil", PLAN.md §3); use `temps.sensors["<i>"]` resp. `temps.heaters["<i>"]`, or `temps.chamber` |
| new: heater load | for every `meta.heaters` with `role: "nozzle"`: `loadStats["<heater>"].mean` (load at the setpoint, 0…1) and `.shareHigh` (time share of the 60 s mean ≥ `meta.heaterLoad.high`); range `[0, 1]` |
| new: MFM % | `fmStats["0"].mean` (lastPercentage) and `fmStats["0"].avgPercentage` |

`currentIndex`: the last layer for a finished job.

## 4. Top view and timelapse: QA's own components

The analysis page does not draw QA's toolpath or fetch its frames itself. QA registers two
components with DWC 3.7's `registerEmbeddableComponent` (the registry for widgets of flexible
layouts, DuetWebControl v3.7-dev `src/plugins/index.ts`, 2026-09-28) when DWC loads the plugin,
and the page renders them by id from `useUiStore().embeddableComponents`:

| id | shows |
|---|---|
| `QualityAssurance.LayerReplay` | the layer's toolpath coloured by commanded flow, the planned extrusion rate of the executed moves as dots (sampled from `move.currentMove.extrusionRate`, not measured), the layer's events as rings (like the Replay tab); wheel zooms, drag pans, double click fits |
| `QualityAssurance.LayerTimelapse` | the camera frame after the layer (`job/timelapse/*`: the video once encoded, the JPEG before); a layer without its own frame shows the latest earlier one; asks again every 5 s while the job is recorded or encoded |

Props of both: `jobId` (QA job id) and `layer` (`job.layer` numbering, i.e. QA's `layers[].layer`,
not an array index: a job QA joined late starts above 1). Without `jobId` they follow
`plugins.QualityAssurance.data.currentJobId`, else `lastJobId`; without `layer` the last layer.
They fill the height their parent gives them (`height: 100%`), talk to the daemon themselves
through DWC's connector, and load a layer only once the page's layer has stayed for 150 ms, so a
slider drag asks for one layer, not for every layer it passes. Messages (index still building,
file gone or changed, no timelapse and why) come from QA's translations.

The CHX 350 analysis offers them as the views "Draufsicht" and "Zeitraffer" beside its layer
strip, when both the embeddable and a QA job id exist: the job opened from the history, or while
printing the one in `currentJobId`.

Findings and the inspection report: Quality Control, not QA.

## 5. Heater-load banner

Stays live in the browser (`stores/heaterLoad.ts`), works without QA. QA records the same quantity
with the same constants (0.8 / 0.9 / 0.05 hysteresis / 60 s / 75 % coverage / 2 K); if one side
changes them, change the other (QA: `dsf/qa_settings.py` `heaterLoad`, `dsf/qa_heaterload.py`).

## 6. Test

Mock `machine/QualityAssurance/jobs` and `job/layers` with the fixtures of `tests/test_api.py`
(or a recorded answer from the machine) in the fork's CHX350 tests.
