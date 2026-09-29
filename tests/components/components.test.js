import { mountInDwc } from "dwc-plugin-test-kit";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoVueWarnings } from "../helpers.js";

/**
 * Every component mounted against real Vuetify 4 (a wrong prop or component name is valid
 * markup that only the running component notices). happy-dom has no 2D canvas, so Chart.js is
 * replaced by a recorder; the configs themselves are tested in tests/core/core.test.js.
 */
const charts = [];
vi.mock("../../src/core/charts", async (importOriginal) => {
	const actual = await importOriginal();
	return {
		...actual,
		registerChartPlugins: () => {},
		Chart: class {
			constructor(canvas, config) {
				this.config = config;
				this.data = config.data;
				this.options = config.options;
				this.destroyed = false;
				charts.push(this);
			}
			update() { this.updates = (this.updates ?? 0) + 1; }
			destroy() { this.destroyed = true; }
		}
	};
});

const ChartCanvas = (await import("../../src/components/ChartCanvas.vue")).default;
const JobList = (await import("../../src/components/JobList.vue")).default;
const JobDetail = (await import("../../src/components/JobDetail.vue")).default;
const EventList = (await import("../../src/components/EventList.vue")).default;
const ContextTable = (await import("../../src/components/ContextTable.vue")).default;
const LayerCharts = (await import("../../src/components/LayerCharts.vue")).default;
const ChannelChart = (await import("../../src/components/ChannelChart.vue")).default;
const TrendsView = (await import("../../src/components/TrendsView.vue")).default;
const SettingsForm = (await import("../../src/components/SettingsForm.vue")).default;
const LivePanel = (await import("../../src/components/LivePanel.vue")).default;
const ObjectModelView = (await import("../../src/components/ObjectModelView.vue")).default;
const QualityAssurance = (await import("../../src/QualityAssurance.vue")).default;

const flush = () => new Promise((r) => setTimeout(r, 0));

const JOB = {
	id: "20260926-100000-abcdef12", file: "0:/gcodes/cube.gcode", result: "finished", printTimeS: 3725,
	timestamp: "2026-09-26T11:02:05Z", analysable: true, qaResult: "completed", startedAt: "2026-09-26T10:00:00Z",
	endedAt: "2026-09-26T11:02:05Z", partial: false, numLayers: 3, material: "PLA", rawPruned: false,
	summary: { abortReason: null, layers: 3, events: { job_start: 1, heater_load: 2 }, filamentRatio: { 0: 0.97 },
		avgPercentage: { 0: 97 }, heaterLoadMean: { 1: 0.55 }, heaterLoadEvents: { 1: 2 }, spoolUsageG: { 0: 42 } }
};

const DETAIL = {
	...JOB, fileCrc32: "deadbeef", startLayer: null, durationS: 3725, warmupS: 120, pauseS: 0,
	context: { heaters: [{ index: 0, role: "bed" }, { index: 1, role: "nozzle", tool: 0 }], file: { crc32: "deadbeef" } },
	summary: { result: "completed", filament: { 0: { ratio: 0.97, avgPercentage: 97, percentDistribution: [{ from: 96, to: 98, s: 10 }] } },
		thermal: { heaterLoad: { 1: { nozzle: true, mean: 0.55 } } }, events: { heater_load: { count: 2 } } }
};

const LAYERS = {
	jobId: JOB.id,
	meta: { sensors: [{ index: 0, name: "bed", type: "thermistor" }, { index: 2, name: "SZP coil", type: "pt1000" }],
		heaters: [{ index: 0, role: "bed", tool: null, sensor: 0, sensorName: "bed" }, { index: 1, role: "nozzle", tool: 0, sensor: 1, sensorName: "nozzle" }],
		chamber: ["sensor", 2], filamentDiameters: { 0: 2.85 }, heaterLoad: { high: 0.8, limit: 0.9 } },
	layers: [1, 2, 3].map((layer) => ({
		layer, startedAt: null, endedAt: null, durationS: 30 + layer, height: 0.2, z: 0.2 * layer, fractionPrinted: 0.3,
		filament: { 0: { commandedMm: 10, measuredMm: 9.7, extruderMm: 10.1, ratio: 0.97 } }, flow: { 0: 2.1 },
		temps: { heaters: { 1: { min: 219, max: 221, mean: 220, std: 0.5, setpoint: 220 } }, sensors: { 2: { min: 30, max: 31, mean: 30.5, std: 0.2, name: "SZP coil" } }, chamber: null },
		fmStats: { 0: { min: 90, max: 104, mean: 97, std: 3, avgPercentage: 97, mmPerRev: 25.1 } },
		pwmStats: {}, loadStats: { 1: { mean: 0.55, max: 0.7, p95: 0.66, maxMean60: 0.6, atSetpointS: 30, shareHigh: 0.1, shareLimit: 0, setpoint: 220 } },
		filamentPath: layer === 1 ? null : { mm: 12, netMm: 1.6, gearPasses: 2 + 2.5 * layer, stepsPerMm: 801, motorSteps: 9612,
			retraction: { length: 0.4, extraRestart: 0 } },
		// layer 1 recorded before schema version 3; the MFM's M92 801 → 834.38 in layer 3
		...(layer === 1 ? {} : { feed: { 0: layer === 2
			? { factor: 1, min: 1, max: 1, stepsPerMm: 801, extrusionFactor: 1, reference: 801 }
			: { factor: 1.0208, min: 1, max: 1.0417, stepsPerMm: 817.69, extrusionFactor: 1, reference: 801 } } })
	}))
};

const EVENTS = [
	{ id: 1, ts_ms: Date.parse(JOB.startedAt), ts: JOB.startedAt, end_ms: null, type: "job_start", subtype: null, layer: null, x: 0, y: 0, z: 0, positions: {}, workplace: 0, offsets: {}, tool: 0, object_id: null, device: null, payload: {}, block_id: 1 },
	{ id: 2, ts_ms: Date.parse(JOB.startedAt) + 125000, ts: "", end_ms: null, type: "heater_load", subtype: "high", layer: 2, x: 10, y: 20, z: 0.4, positions: {}, workplace: 0, offsets: {}, tool: 0, object_id: null, device: 1, payload: { peakMean: 0.86, setpoint: 220 }, block_id: 2 }
];

function fakeApi(overrides = {}) {
	return {
		status: vi.fn(async () => ({ collector: { state: "idle", currentJobId: null, lastJobId: null, layer: null },
			accelerometer: { enabled: false, available: [{ index: 0, port: "60.i2c.lis", board: 60 }] } })),
		jobs: vi.fn(async () => ({ total: 1, offset: 0, jobs: [JOB] })),
		job: vi.fn(async () => DETAIL),
		layers: vi.fn(async () => LAYERS),
		events: vi.fn(async () => ({ jobId: JOB.id, events: EVENTS })),
		channels: vi.fn(async () => ({ channels: ["heater.1.current", "fm.0.lastPercentage"], derived: ["heater.1.load"] })),
		samples: vi.fn(async (id, channels) => ({ jobId: id, from: 0, to: 1, resolution: "auto", downsampled: false,
			channels: Object.fromEntries(channels.map((c) => [c, [[Date.parse(JOB.startedAt), 1], [Date.parse(JOB.startedAt) + 5000, 2]]])) })),
		// newest first, as the daemon answers
		trends: vi.fn(async (metric) => ({ metric, points: [
			{ jobId: "b", ts: "2026-09-25T10:00:00Z", result: "completed", material: "PLA", value: 0.55, heater: 1, tool: 0, setpoint: 220, nozzleDiameter: 0.8 },
			{ jobId: "a", ts: "2026-09-20T10:00:00Z", result: "completed", material: "PLA", value: 0.5, heater: 1, tool: 0, setpoint: 220, nozzleDiameter: 0.8 }
		] })),
		settings: vi.fn(async () => ({ settings: { sampleIntervalS: 5, ringBufferS: 90, postTriggerS: 30,
			thresholds: { temperatureK: 5, filamentPercentPoints: 15, vInPercent: 10, phantomJumpK: 15, phantomReturnS: 5 },
			filamentPercentWindowMinS: 5, heaterLoad: { high: 0.8, limit: 0.9 }, chamber: { mode: "auto", index: null, autoSensorName: "SZP coil" },
			contextGlobals: ["nozzle_diameter"], machineSignals: { mfm: true }, timelapse: { enabled: true, snapshotUrl: null },
			retention: { jobs: 50, days: 90, maxDbBytes: 1 } }, errors: [] })),
		saveSettings: vi.fn(async (s) => ({ saved: true, settings: s, errors: [] })),
		exportBlob: vi.fn(async () => new Blob(["{}"])),
		...overrides
	};
}

let warn;
beforeEach(() => {
	charts.length = 0;
	warn = vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => {
	vi.restoreAllMocks();
});

describe("ChartCanvas", () => {
	it("creates once, updates in place, destroys on unmount", async () => {
		const config = (y) => ({ type: "line", data: { datasets: [{ data: [{ x: 0, y }] }] }, options: {} });
		const wrapper = mountInDwc(ChartCanvas, { props: { config: config(1) } });
		expect(charts).toHaveLength(1);
		await wrapper.setProps({ config: config(2) });
		expect(charts).toHaveLength(1);
		expect(charts[0].updates).toBe(1);
		wrapper.unmount();
		expect(charts[0].destroyed).toBe(true);
		expectNoVueWarnings(warn);
	});

	it("shows a placeholder without data", () => {
		const wrapper = mountInDwc(ChartCanvas, { props: { config: { type: "line", data: { datasets: [] }, options: {} } } });
		expect(wrapper.text()).toContain("plugins.QualityAssurance.common.noData");
		expect(charts).toHaveLength(0);
	});
});

describe("JobList", () => {
	it("lists jobs and emits the selection", async () => {
		const api = fakeApi();
		const wrapper = mountInDwc(JobList, { props: { api } });
		await flush();
		expect(api.jobs).toHaveBeenCalledWith({ limit: 25, offset: 0, result: undefined, material: undefined });
		const text = wrapper.text();
		expect(text).toContain("cube.gcode");
		expect(text).toContain("1h 02m");
		expect(text).toContain("97.0 %");
		expect(text).toContain("55 %");
		await wrapper.find("tbody tr").trigger("click");
		expect(wrapper.emitted("select")).toEqual([[JOB.id]]);
		expectNoVueWarnings(warn);
	});

	it("shows an error", async () => {
		const wrapper = mountInDwc(JobList, { props: { api: fakeApi({ jobs: vi.fn(async () => { throw new Error("offline"); }) }) } });
		await flush();
		expect(wrapper.text()).toContain("offline");
	});
});

describe("JobDetail", () => {
	it("loads a job with layers and events", async () => {
		const api = fakeApi();
		const wrapper = mountInDwc(JobDetail, { props: { api, jobId: JOB.id } });
		await flush();
		await flush();
		expect(api.job).toHaveBeenCalledWith(JOB.id);
		const text = wrapper.text();
		expect(text).toContain("cube.gcode");
		expect(text).toContain("97.0 %");
		expect(text).toContain("plugins.QualityAssurance.results.completed");
		expect(charts.length).toBeGreaterThan(0);
		expectNoVueWarnings(warn);
	});

	it("reloads without a progress bar when the page bumps the key", async () => {
		const api = fakeApi();
		const wrapper = mountInDwc(JobDetail, { props: { api, jobId: JOB.id } });
		await flush();
		await flush();
		await wrapper.setProps({ reloadKey: 1 });
		expect(wrapper.vm.loading).toBe(false);  // the job stays on screen, no progress bar above it
		await flush();
		expect(api.job).toHaveBeenCalledTimes(2);
		expect(api.layers).toHaveBeenCalledTimes(2);
		expect(api.events).toHaveBeenCalledTimes(2);
		expectNoVueWarnings(warn);
	});

	it("shows no end while the job runs", async () => {
		const api = fakeApi({ job: vi.fn(async () => ({ ...DETAIL, endedAt: null, qaResult: "running", summary: null })) });
		const wrapper = mountInDwc(JobDetail, { props: { api, jobId: JOB.id } });
		await flush();
		await flush();
		expect(wrapper.text()).toContain(JOB.id);
		expect(wrapper.text()).not.toContain("–");
		expectNoVueWarnings(warn);
	});
});

describe("EventList", () => {
	it("shows events with elapsed time and filters by type", async () => {
		const wrapper = mountInDwc(EventList, { props: { events: EVENTS, startMs: Date.parse(JOB.startedAt) } });
		expect(wrapper.text()).toContain("2:05");
		expect(wrapper.text()).toContain("86 % @ 220 °C");
		expect(wrapper.findAll("tbody tr")).toHaveLength(2);
		await wrapper.findAll(".v-chip")[0].trigger("click");
		expect(wrapper.findAll("tbody tr").length).toBe(1);
		expectNoVueWarnings(warn);
	});

	it("marks a driver's open load that cleared within 500 ms", () => {
		const base = { ...EVENTS[1], type: "driver_error", subtype: "warning", device: 0 };
		const events = [
			{ ...base, id: 3, payload: { canAddress: 20, driver: 0, bits: ["phase B may be disconnected"], confirmed: false, durationS: 0.3 } },
			{ ...base, id: 4, payload: { canAddress: 20, driver: 0, bits: ["phase A may be disconnected"], confirmed: true, durationS: 2.1 } }
		];
		const wrapper = mountInDwc(EventList, { props: { events, startMs: Date.parse(JOB.startedAt) } });
		const rows = wrapper.findAll("tbody tr");
		expect(rows[0].text()).toContain("plugins.QualityAssurance.events.unconfirmed");
		expect(rows[0].text()).toContain("20.0: phase B may be disconnected, 0.3 s");
		expect(rows[1].text()).not.toContain("plugins.QualityAssurance.events.unconfirmed");
		expectNoVueWarnings(warn);
	});
});

describe("ContextTable", () => {
	it("flattens nested objects", () => {
		const wrapper = mountInDwc(ContextTable, { props: { data: { file: { crc32: "deadbeef" }, tools: [{ number: 0 }], list: [1, 2] } } });
		const text = wrapper.text();
		expect(text).toContain("crc32");
		expect(text).toContain("deadbeef");
		expect(text).toContain("[0]");
		expect(text).toContain("1, 2");
		expectNoVueWarnings(warn);
	});

	it("gives a mesh's heights as their size", () => {
		const heights = [[-0.115, -0.12, null], [-0.492, 0, 0.2]];
		const wrapper = mountInDwc(ContextTable, { props: { data: { calibration: { mesh: { points: 5, heights } } } } });
		const text = wrapper.text();
		expect(text).toContain("2 × 3");
		expect(text).not.toContain("-0.492");
		expectNoVueWarnings(warn);
	});
});

describe("ObjectModelView", () => {
	const start = Date.parse(JOB.startedAt);
	const journal = { bytes: 5_300_000, start, end: start + 3_600_000, snapshots: 7 };

	it("says when a job has no journal", () => {
		const wrapper = mountInDwc(ObjectModelView, { props: { api: fakeApi(), job: { ...DETAIL, journal: null } } });
		expect(wrapper.text()).toContain("plugins.QualityAssurance.om.none");
		expectNoVueWarnings(warn);
	});

	it("shows the model at a time and the history of a value", async () => {
		const api = fakeApi({
			objectModel: vi.fn(async (id, at, path) => ({ at, snapshotAt: start + 3_000_000, path,
				value: { meshDeviation: { mean: -0.121, deviation: 0.145 } } })),
			objectModelHistory: vi.fn(async (id, path) => ({ path, truncated: false, points: [
				{ t: start, value: "automatic" }, { t: start + 65_000, value: "default" }] })),
			journalBlob: vi.fn(async () => new Blob(["x"]))
		});
		const wrapper = mountInDwc(ObjectModelView, { props: { api, job: { ...DETAIL, journal } } });
		expect(wrapper.text()).toContain("plugins.QualityAssurance.om.download");
		wrapper.vm.path = "move.compensation";
		await wrapper.vm.show();
		expect(api.objectModel).toHaveBeenCalledWith(JOB.id, start + 3_600_000, "move.compensation");
		await flush();
		expect(wrapper.text()).toContain("meshDeviation");
		expect(wrapper.text()).toContain("-0.121");

		wrapper.vm.path = "global.machine_mode";
		await wrapper.vm.loadHistory();
		await flush();
		const rows = wrapper.findAll(".qa-om-point");
		expect(rows.map((r) => r.text())).toEqual(["0:00automatic", "1:05default"]);
		await rows[1].trigger("click");
		expect(wrapper.vm.at).toBe(start + 65_000);
		expect(api.objectModel).toHaveBeenLastCalledWith(JOB.id, start + 65_000, "global.machine_mode");
		expectNoVueWarnings(warn);
	});

	it("reports a failed replay", async () => {
		const api = fakeApi({ objectModel: vi.fn(async () => { throw new Error("no object model journal for this job"); }) });
		const wrapper = mountInDwc(ObjectModelView, { props: { api, job: { ...DETAIL, journal } } });
		await wrapper.vm.show();
		await flush();
		expect(wrapper.text()).toContain("no object model journal for this job");
		expectNoVueWarnings(warn);
	});
});

describe("LayerCharts", () => {
	it("switches views", async () => {
		const wrapper = mountInDwc(LayerCharts, { props: { answer: LAYERS } });
		expect(charts[0].data.datasets[0].data).toEqual([31, 32, 33]);
		for (const view of ["heaterLoad", "monitor", "filament", "flow", "temperatures"]) {
			wrapper.vm.view = view;
			await flush();
		}
		const last = charts[charts.length - 1];
		expect(last.data.datasets[0].label).toContain("SZP coil");
		wrapper.vm.view = "gearPasses";   // layer 1 finished before the layer index was ready
		await flush();
		expect(charts[charts.length - 1].data.datasets[0].data).toEqual([null, 7, 9.5]);
		expect(wrapper.text()).toContain("plugins.QualityAssurance.layers.gearPassesHint");
		wrapper.vm.view = "feed";
		await flush();
		expect(charts[charts.length - 1].data.datasets.map((d) => [d.label, d.data])).toEqual(
			[["plugins.QualityAssurance.layers.feed #0 (%)", [null, 100, 102.1]]]);
		expect(wrapper.text()).toContain("plugins.QualityAssurance.layers.feedHint");
		expectNoVueWarnings(warn);
	});

	it("says when there are no layers", () => {
		const wrapper = mountInDwc(LayerCharts, { props: { answer: { ...LAYERS, layers: [] } } });
		expect(wrapper.text()).toContain("plugins.QualityAssurance.layers.none");
	});
});

describe("ChannelChart", () => {
	it("loads the default channels that exist", async () => {
		const api = fakeApi();
		mountInDwc(ChannelChart, { props: { api, jobId: JOB.id, startMs: Date.parse(JOB.startedAt), events: EVENTS,
			defaultChannels: ["heater.1.current", "heater.1.load", "heater.7.current"] } });
		await flush();
		await flush();
		expect(api.samples).toHaveBeenCalledWith(JOB.id, ["heater.1.current", "heater.1.load"], { resolution: "auto" });
		const chart = charts[charts.length - 1];
		expect(chart.data.datasets.find((d) => d.label.startsWith("heater.1.load")).yAxisID).toBe("y2");
		expect(chart.options.plugins.qaMarkers.markers).toHaveLength(1);
		expectNoVueWarnings(warn);
	});
});

describe("TrendsView", () => {
	it("groups heater load by tool, setpoint and nozzle", async () => {
		const api = fakeApi();
		mountInDwc(TrendsView, { props: { api } });
		await flush();
		expect(api.trends).toHaveBeenCalledWith("heater_load_mean", { limit: 200, material: undefined });
		const chart = charts[charts.length - 1];
		expect(chart.data.datasets).toHaveLength(1);
		expect(chart.data.datasets[0].label).toBe("T0 220 °C ⌀0.8");
		expect(chart.data.datasets[0].data.map((p) => p.y)).toEqual([50, 55]);
		expectNoVueWarnings(warn);
	});
});

describe("SettingsForm", () => {
	it("edits and saves", async () => {
		const api = fakeApi();
		const wrapper = mountInDwc(SettingsForm, { props: { api } });
		await flush();
		wrapper.vm.set("postTriggerS", 45);
		await wrapper.findAll("button").find((b) => b.text().includes("settings.save")).trigger("click");
		await flush();
		expect(api.saveSettings.mock.calls[0][0].postTriggerS).toBe(45);
		expect(wrapper.text()).toContain("plugins.QualityAssurance.settings.saved");
		expect(wrapper.text()).toContain("plugins.QualityAssurance.settings.accelAvailable");
		expectNoVueWarnings(warn);
	});

	it("says why the timelapse is not ready", async () => {
		const api = fakeApi();
		const status = await api.status();
		api.status = vi.fn(async () => ({ ...status, timelapse: { enabled: false, reason: "ffmpeg not found" } }));
		const wrapper = mountInDwc(SettingsForm, { props: { api } });
		await flush();
		expect(wrapper.text()).toContain("plugins.QualityAssurance.settings.timelapseNotReady");
		wrapper.vm.set("timelapse.enabled", false);
		await flush();
		expect(wrapper.text()).not.toContain("plugins.QualityAssurance.settings.timelapseNotReady");
		expectNoVueWarnings(warn);
	});

	it("shows what the daemon refused", async () => {
		const api = fakeApi({ saveSettings: vi.fn(async (s) => ({ saved: false, settings: s, errors: ["sampleIntervalS: must be >= 1"] })) });
		const wrapper = mountInDwc(SettingsForm, { props: { api } });
		await flush();
		await wrapper.findAll("button").find((b) => b.text().includes("settings.save")).trigger("click");
		await flush();
		// next to the Save button (the form is longer than the screen), and on the field itself
		const alert = wrapper.findAll(".v-alert").find((a) => a.text().includes("sampleIntervalS: must be >= 1"));
		expect(alert.element.nextElementSibling.textContent).toContain("settings.save");
		const field = wrapper.findAll(".v-input--error");
		expect(field).toHaveLength(1);
		expect(field[0].text()).toContain("settings.fields.sampleIntervalS");
		expect(field[0].text()).toContain("must be >= 1");
		// editing the field takes its refusal back
		wrapper.vm.set("sampleIntervalS", 5);
		await flush();
		expect(wrapper.findAll(".v-input--error")).toHaveLength(0);
		expectNoVueWarnings(warn);
	});
});

describe("LivePanel", () => {
	it("follows the live frames", async () => {
		const wrapper = mountInDwc(LivePanel, { props: { connection: "open" } });
		expect(wrapper.text()).toContain("plugins.QualityAssurance.live.idle");
		expect(wrapper.find(".text-caption").text()).toBe("plugins.QualityAssurance.live.states.open");
		await wrapper.setProps({ frame: { type: "sample", ts: 1, jobId: "j", layer: 4, values: { "fm.0.lastPercentage": 98 },
			heaterLoad: { 1: { mean: 0.86, level: "high" } } } });
		expect(wrapper.text()).toContain("plugins.QualityAssurance.live.recording");
		expect(wrapper.find(".text-caption").text()).toBe("j · plugins.QualityAssurance.live.layer · plugins.QualityAssurance.live.states.open");
		expect(wrapper.text()).toContain("86 %");
		expect(wrapper.text()).toContain("98 %");
		await wrapper.setProps({ frame: { type: "event", ts: 2, event: { type: "heater_load", subtype: "high" } } });
		expect(wrapper.text()).toContain("plugins.QualityAssurance.eventTypes.heater_load");
		expectNoVueWarnings(warn);
	});
});

describe("QualityAssurance page", () => {
	it("mounts with a fake host and polls without a socket", async () => {
		const host = {
			pluginEntry: () => ({ pid: 42 }),
			startBackend: () => Promise.resolve(),
			webSocketUrl: () => null,
			request: vi.fn(async (method, path) => {
				if (path.endsWith("/status")) {
					return { collector: { state: "recording", currentJobId: "j", lastJobId: null, layer: 7 } };
				}
				if (path.endsWith("/jobs")) {
					return { total: 1, offset: 0, jobs: [JOB] };
				}
				return {};
			})
		};
		const wrapper = mountInDwc(QualityAssurance, { props: { host } });
		await flush();
		await flush();
		expect(host.request.mock.calls.some(([, path]) => path === "machine/QualityAssurance/status")).toBe(true);
		expect(wrapper.text()).toContain("plugins.QualityAssurance.live.recording");
		expect(wrapper.text()).toContain("cube.gcode");
		wrapper.unmount();
		expectNoVueWarnings(warn);
	});

	it("reloads a running job's detail on its layer changes, at most every 30 s", async () => {
		vi.useFakeTimers({ now: 1_000_000 });
		try {
			const host = { pluginEntry: () => ({ pid: 42 }), startBackend: () => Promise.resolve(), webSocketUrl: () => null,
				request: vi.fn(async () => ({})) };
			const wrapper = mountInDwc(QualityAssurance, { props: { host } });
			const vm = wrapper.vm;
			vm.openJob("j");
			const version = vm.jobVersion;
			vm.onFrame({ type: "layer", ts: 1, jobId: "other", layer: 2 });
			vm.onFrame({ type: "layer", ts: 1, jobId: "j", layer: 2 });
			expect(vm.jobVersion).toBe(version);  // just opened: the detail has loaded it
			vm.onFrame({ type: "layer", ts: 2, jobId: "j", layer: 3 });
			vi.advanceTimersByTime(29_999);
			expect(vm.jobVersion).toBe(version);
			vi.advanceTimersByTime(1);
			expect(vm.jobVersion).toBe(version + 1);  // one reload for both changes
			vi.advanceTimersByTime(60_000);
			vm.onFrame({ type: "layer", ts: 3, jobId: "j", layer: 4 });
			expect(vm.jobVersion).toBe(version + 2);  // quiet for 30 s: at once
			vm.onFrame({ type: "layer", ts: 4, jobId: "j", layer: 5 });
			vm.onFrame({ type: "job", ts: 5, jobId: "j" });
			expect(vm.jobVersion).toBe(version + 3);  // the end reloads at once and takes the pending reload along
			vi.advanceTimersByTime(60_000);
			expect(vm.jobVersion).toBe(version + 3);
			wrapper.unmount();
		} finally {
			vi.useRealTimers();
		}
	});
});
