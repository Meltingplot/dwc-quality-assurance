# CHX 350 UI: history and analysis from QA

Work order for the DuetWebControl fork (`Meltingplot/DuetWebControl`, `src/plugins/CHX350`). QA does
not change the fork; it provides the API (docs/api.md) and pins the contract in
`tests/test_api.py` (`test_jobs_contract_for_the_chx_ui`, `test_layers_contract_for_the_chx_analysis`).
Read against the fork's v3.7-dev @ 1fb6a51 on 2026-09-26 (PLAN.md §5.10).

## 1. QA is running

Same rule as the CHX350 backend (`api.ts` `backendAvailable`): the pid in the object model, not
a probe request, because DSF keeps a crashed daemon's endpoints registered.

```ts
export const qaAvailable = computed(() => (useMachineStore().model.plugins.get("QualityAssurance")?.pid ?? -1) > 0);
```

Requests like the existing `get<T>()` in `api.ts`: `useMachineStore().request("GET", "machine/QualityAssurance/<path>", params, "json", null, 15000, …)`.

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

## 4. Placeholders of the analysis page

- Part view: the replay — `GET machine/QualityAssurance/job/toolpath?id&layer` (docs/api.md "Toolpath";
  202 while QA builds the layer index, 409 when the file is gone or was changed).
- Timelapse: `job/timelapse*` (QA phase 5).
- Findings, inspection report: Quality Control, not QA.

## 5. Heater-load banner

Stays live in the browser (`stores/heaterLoad.ts`), works without QA. QA records the same quantity
with the same constants (0.8 / 0.9 / 0.05 hysteresis / 60 s / 75 % coverage / 2 K); if one side
changes them, change the other (QA: `dsf/qa_settings.py` `heaterLoad`, `dsf/qa_heaterload.py`).

## 6. Test

Mock `machine/QualityAssurance/jobs` and `job/layers` with the fixtures of `tests/test_api.py`
(or a recorded answer from the machine) in the fork's CHX350 tests.
