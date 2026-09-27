import { mountInDwc } from "dwc-plugin-test-kit";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoVueWarnings } from "../helpers.js";

vi.mock("../../src/core/charts", async (importOriginal) => {
	const actual = await importOriginal();
	return {
		...actual,
		registerChartPlugins: () => {},
		Chart: class {
			constructor(canvas, config) { this.config = config; this.data = config.data; this.options = config.options; }
			update() {}
			destroy() {}
		}
	};
});

const ReplayView = (await import("../../src/components/ReplayView.vue")).default;
const ReplayCanvas = (await import("../../src/components/ReplayCanvas.vue")).default;
const TimelapseFrame = (await import("../../src/components/TimelapseFrame.vue")).default;

const flush = () => new Promise((r) => setTimeout(r, 0));

const TOOLPATH = {
	layer: 1,
	segments: { x0: [0], y0: [0], x1: [10], y1: [0], z: [0.2], e: [1], flow: [3], object: [0], type: [0], travel: [0] },
	types: ["Perimeter"], objects: { 0: "cube" },
	meta: { numLayers: 3, source: "comments", objects: { 0: "cube" }, filamentDiameter: 2.85 }
};

const JOB = {
	id: "j1", file: "0:/gcodes/a.gcode", numLayers: 3, rawPruned: false, startedAt: "2026-09-26T10:00:00Z",
	context: { axes: [{ letter: "X" }, { letter: "Y" }], tools: [{ number: 0, offsets: [-2, 3] }] }
};
const T0 = Date.parse("2026-09-26T10:00:00Z");
const T1 = Date.parse("2026-09-26T10:01:00Z");  // layer 1 starts

const LAYERS = {
	jobId: "j1",
	meta: { sensors: [], heaters: [{ index: 1, role: "nozzle", tool: 0 }], chamber: null, filamentDiameters: {}, heaterLoad: { high: 0.8, limit: 0.9 } },
	layers: [1, 2, 3].map((layer) => ({ layer, startedAt: `2026-09-26T10:0${layer}:00Z`, endedAt: `2026-09-26T10:0${layer}:30Z` }))
};

// the job starts before a tool is selected (tool −1); the tool comes from the next event
const EVENTS = [
	{ id: 1, ts_ms: T0, type: "job_start", subtype: null, layer: null, x: 0, y: 0, z: 0, offsets: { X: 10, Y: 0 }, tool: -1, object_id: null, payload: {} },
	{ id: 7, ts_ms: T1 + 2000, type: "heater_load", subtype: "high", layer: 1, x: 5, y: 1, z: 0.2, offsets: { X: 10, Y: 0 }, tool: 0, object_id: 0, payload: {} }
];

let warn;
beforeEach(() => {
	warn = vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => {
	vi.restoreAllMocks();
	vi.useRealTimers();
});

function api(toolpath, timelapse = { jobId: "j1", status: "none", reason: "no snapshotUrl set", video: false, layers: [] }) {
	return {
		toolpath: vi.fn(toolpath),
		timelapseMeta: vi.fn(async () => timelapse),
		timelapseFrame: vi.fn(async () => new Blob(["jpeg"])),
		// positions only in the coarse row before the layer, a rate sample before it that is not the layer's
		samples: vi.fn(async (id) => ({ jobId: id, from: 0, to: 1, resolution: "auto", downsampled: false,
			channels: { "axis.X.machinePosition": [[T1 - 3000, 2]], "axis.Y.machinePosition": [[T1 - 3000, 0]],
				"move.currentMove.extrusionRate": [[T1 - 1000, 0.4], [T1 + 500, 0.5]],
				"heater.1.current": [[T1 - 3000, 219], [T1 + 1000, 220]], "heater.1.load": [[T1 + 1000, 0.6]] } }))
	};
}

describe("ReplayView", () => {
	it("draws the layer with measured points and event markers", async () => {
		const a = api(async () => TOOLPATH);
		const wrapper = mountInDwc(ReplayView, { props: { api: a, job: JOB, layers: LAYERS, events: EVENTS } });
		await flush();
		await flush();
		expect(a.toolpath).toHaveBeenCalledWith("j1", 1);
		expect(a.samples.mock.calls[0][1]).toEqual(["axis.X.machinePosition", "axis.Y.machinePosition",
			"move.currentMove.extrusionRate", "heater.1.current", "heater.1.load"]);
		expect(a.samples.mock.calls[0][2]).toEqual({ from: T1 - 10000, to: T1 + 30000, resolution: "auto" });
		const canvas = wrapper.findComponent(ReplayCanvas);
		// user = machine − workplace offset + tool offset of T0
		expect(canvas.props("measured")).toEqual([{ x: 2 - 10 - 2, y: 0 + 3, flow: 0.5 * Math.PI * (2.85 / 2) ** 2 }]);
		expect(canvas.props("markers")).toMatchObject([{ x: 5 - 10 - 2, y: 1 + 3 }]);
		expect(wrapper.text()).toContain("cube");
		expect(wrapper.text()).toContain("plugins.QualityAssurance.eventTypes.heater_load");
		// curves start at the layer, with its events as markers
		const config = wrapper.vm.curvesConfig;
		expect(config.data.datasets.map((d) => d.label)).toEqual(["T0 (°C)", "plugins.QualityAssurance.layers.loadMean T0 (0..1)"]);
		expect(config.data.datasets[0].data).toEqual([{ x: 1, y: 220 }]);
		expect(config.options.plugins.qaMarkers.markers).toMatchObject([{ x: 2, label: "heater_load" }]);
		expectNoVueWarnings(warn);
	});

	it("shows the camera frame of the layer when there is a timelapse", async () => {
		vi.spyOn(URL, "createObjectURL").mockImplementation(() => "blob:frame");
		vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
		const plain = mountInDwc(ReplayView, { props: { api: api(async () => TOOLPATH), job: JOB, layers: LAYERS, events: [] } });
		await flush();
		expect(plain.findComponent(TimelapseFrame).exists()).toBe(false);
		const timelapse = { jobId: "j1", status: "capturing", fps: 30, video: false,
			layers: [{ layer: 1, frame: 0, ts: 1 }, { layer: 2, frame: 1, ts: 2 }] };
		const a = api(async () => TOOLPATH, timelapse);
		const wrapper = mountInDwc(ReplayView, { props: { api: a, job: JOB, layers: LAYERS, events: [] } });
		await flush();
		await flush();
		expect(wrapper.findComponent(TimelapseFrame).props("layer")).toBe(1);
		expect(a.timelapseFrame).toHaveBeenCalledWith("j1", 1);
		wrapper.vm.step(1);
		await flush();
		await flush();
		expect(a.timelapseFrame).toHaveBeenLastCalledWith("j1", 2);
		expectNoVueWarnings(warn);
	});

	it("hides the events of a hidden object", async () => {
		const a = api(async () => TOOLPATH);
		const wrapper = mountInDwc(ReplayView, { props: { api: a, job: JOB, layers: LAYERS, events: EVENTS } });
		await flush();
		await flush();
		wrapper.vm.toggleObject(0, false);
		await flush();
		const canvas = wrapper.findComponent(ReplayCanvas);
		expect(canvas.props("hiddenObjects")).toEqual([0]);
		expect(canvas.props("markers")).toEqual([]);
		wrapper.vm.toggleObject(0, true);
		await flush();
		expect(canvas.props("markers")).toHaveLength(1);
	});

	it("waits while the index is built, then loads", async () => {
		vi.useFakeTimers();
		let calls = 0;
		const a = api(async () => (++calls === 1 ? { state: "building" } : TOOLPATH));
		const wrapper = mountInDwc(ReplayView, { props: { api: a, job: JOB, layers: LAYERS, events: [] } });
		await vi.advanceTimersByTimeAsync(0);
		expect(wrapper.text()).toContain("plugins.QualityAssurance.replay.building");
		await vi.advanceTimersByTimeAsync(2100);
		expect(a.toolpath).toHaveBeenCalledTimes(2);
		expect(wrapper.text()).not.toContain("replay.building");
	});

	it("says when the file is gone", async () => {
		const a = api(async () => { throw new Error("bad status code 409"); });
		const wrapper = mountInDwc(ReplayView, { props: { api: a, job: JOB, layers: LAYERS, events: [] } });
		await flush();
		expect(wrapper.text()).toContain("plugins.QualityAssurance.replay.fileGone");
	});

	it("plays through the layers", async () => {
		vi.useFakeTimers();
		const a = api(async () => TOOLPATH);
		const wrapper = mountInDwc(ReplayView, { props: { api: a, job: JOB, layers: LAYERS, events: [] } });
		await vi.advanceTimersByTimeAsync(0);
		wrapper.vm.togglePlay();
		await vi.advanceTimersByTimeAsync(2100);
		expect(wrapper.vm.layer).toBe(3);
		await vi.advanceTimersByTimeAsync(1100);
		expect(wrapper.vm.playing).toBe(false);
	});
});
