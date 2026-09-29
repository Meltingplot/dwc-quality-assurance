import { mountInDwc, setModel } from "dwc-plugin-test-kit";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { registeredEmbeddables } from "../dwc-stubs/plugins.js";
import { expectNoVueWarnings } from "../helpers.js";

const LayerReplay = (await import("../../src/components/LayerReplay.vue")).default;
const LayerTimelapse = (await import("../../src/components/LayerTimelapse.vue")).default;
const ReplayCanvas = (await import("../../src/components/ReplayCanvas.vue")).default;
const TimelapseFrame = (await import("../../src/components/TimelapseFrame.vue")).default;

const flush = async () => {
	for (let i = 0; i < 4; i++) {
		await new Promise((r) => setTimeout(r, 0));
	}
};
/** A layer change the page makes settles before the views ask for it */
const settle = async () => {
	await new Promise((r) => setTimeout(r, 200));
	await flush();
};

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

const EVENTS = [
	{ id: 1, ts_ms: T0, type: "job_start", subtype: null, layer: null, x: 0, y: 0, z: 0, offsets: { X: 10, Y: 0 }, tool: -1, object_id: null, payload: {} },
	{ id: 7, ts_ms: T1 + 2000, type: "heater_load", subtype: "high", layer: 1, x: 5, y: 1, z: 0.2, offsets: { X: 10, Y: 0 }, tool: 0, object_id: 0, payload: {} }
];

const TIMELAPSE = { jobId: "j1", status: "capturing", fps: 30, video: false,
	layers: [{ layer: 1, frame: 0, ts: 1 }, { layer: 2, frame: 1, ts: 2 }] };

function api({ toolpath = async () => TOOLPATH, timelapse = TIMELAPSE } = {}) {
	return {
		job: vi.fn(async () => JOB),
		layers: vi.fn(async () => LAYERS),
		events: vi.fn(async (id) => ({ jobId: id, events: EVENTS })),
		toolpath: vi.fn(toolpath),
		samples: vi.fn(async (id) => ({ jobId: id, from: 0, to: 1, resolution: "auto", downsampled: false,
			channels: { "axis.X.machinePosition": [[T1 - 3000, 2]], "axis.Y.machinePosition": [[T1 - 3000, 0]],
				"move.currentMove.extrusionRate": [[T1 - 1000, 0.4], [T1 + 500, 0.5]] } })),
		timelapseMeta: vi.fn(async () => timelapse),
		timelapseFrame: vi.fn(async () => new Blob(["jpeg"]))
	};
}

let warn;
beforeEach(() => {
	warn = vi.spyOn(console, "warn").mockImplementation(() => {});
	vi.spyOn(URL, "createObjectURL").mockImplementation(() => "blob:frame");
	vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
});
afterEach(() => {
	vi.restoreAllMocks();
	vi.useRealTimers();
});

describe("LayerReplay (embeddable)", () => {
	it("draws the chosen layer of the chosen job with executed points and event markers, filling its parent", async () => {
		const a = api();
		const wrapper = mountInDwc(LayerReplay, { props: { jobId: "j1", layer: 1, api: a } });
		await flush();
		expect(a.toolpath).toHaveBeenLastCalledWith("j1", 1);
		expect(a.samples.mock.calls.at(-1)[2]).toEqual({ from: T1 - 10000, to: T1 + 30000, resolution: "auto" });
		const canvas = wrapper.findComponent(ReplayCanvas);
		expect(canvas.props("height")).toBe(0);
		// user = machine − workplace offset + tool offset of T0 (as in the replay tab)
		expect(canvas.props("executed")).toEqual([{ x: 2 - 10 - 2, y: 0 + 3, flow: 0.5 * Math.PI * (2.85 / 2) ** 2 }]);
		expect(canvas.props("markers")).toMatchObject([{ x: 5 - 10 - 2, y: 1 + 3, label: "heater_load" }]);

		// another layer of the same job: only the layer is asked for
		await wrapper.setProps({ layer: 2 });
		await settle();
		expect(a.toolpath).toHaveBeenLastCalledWith("j1", 2);
		expect(a.job).toHaveBeenCalledTimes(1);
		expect(canvas.props("markers")).toEqual([]);
		expectNoVueWarnings(warn);
	});

	it("asks for the layer a drag stops at, not for every layer it passes", async () => {
		const a = api();
		const wrapper = mountInDwc(LayerReplay, { props: { jobId: "j1", layer: 1, api: a } });
		await flush();
		const before = a.toolpath.mock.calls.length;
		for (const layer of [2, 3, 2, 3]) {
			await wrapper.setProps({ layer });
		}
		await settle();
		expect(a.toolpath.mock.calls.slice(before)).toEqual([["j1", 3]]);
	});

	it("shows the job's last recorded layer without a layer", async () => {
		const a = api();
		mountInDwc(LayerReplay, { props: { jobId: "j1", api: a } });
		await flush();
		expect(a.toolpath).toHaveBeenLastCalledWith("j1", 3);
	});

	it("follows the job QA records without a job id", async () => {
		setModel({ plugins: new Map([["QualityAssurance", { pid: 42, data: new Map([["currentJobId", "j1"], ["lastJobId", "j0"]]) }]]) });
		const a = api();
		mountInDwc(LayerReplay, { props: { layer: 2, api: a } });
		await flush();
		expect(a.job).toHaveBeenCalledWith("j1");
		expect(a.toolpath).toHaveBeenLastCalledWith("j1", 2);
	});

	it("says so while QA has no job", async () => {
		const a = api();
		const wrapper = mountInDwc(LayerReplay, { props: { api: a } });
		await flush();
		expect(a.toolpath).not.toHaveBeenCalled();
		expect(wrapper.text()).toContain("plugins.QualityAssurance.embed.noJob");
	});

	it("waits while the index is built, then draws", async () => {
		vi.useFakeTimers();
		let calls = 0;
		const a = api({ toolpath: async () => (++calls === 1 ? { state: "building" } : TOOLPATH) });
		const wrapper = mountInDwc(LayerReplay, { props: { jobId: "j1", layer: 1, api: a } });
		await vi.advanceTimersByTimeAsync(0);
		expect(wrapper.text()).toContain("plugins.QualityAssurance.replay.building");
		await vi.advanceTimersByTimeAsync(2100);
		expect(wrapper.text()).not.toContain("replay.building");
		expect(wrapper.findComponent(ReplayCanvas).props("toolpath")).not.toBeNull();
	});

	it("says when the file is gone", async () => {
		const a = api({ toolpath: async () => { throw new Error("bad status code 409"); } });
		const wrapper = mountInDwc(LayerReplay, { props: { jobId: "j1", layer: 1, api: a } });
		await flush();
		expect(wrapper.text()).toContain("plugins.QualityAssurance.replay.fileGone");
	});
});

describe("LayerTimelapse (embeddable)", () => {
	it("shows the frame of the chosen layer, filling its parent", async () => {
		const a = api();
		const wrapper = mountInDwc(LayerTimelapse, { props: { jobId: "j1", layer: 2, api: a } });
		await flush();
		const frame = wrapper.findComponent(TimelapseFrame);
		expect(frame.props()).toMatchObject({ jobId: "j1", layer: 2, fill: true });
		expect(a.timelapseFrame).toHaveBeenLastCalledWith("j1", 2);
		await wrapper.setProps({ layer: 1 });
		await settle();
		expect(a.timelapseFrame).toHaveBeenLastCalledWith("j1", 1);
		wrapper.unmount();
		expectNoVueWarnings(warn);
	});

	it("shows the latest frame without a layer", async () => {
		const a = api();
		const wrapper = mountInDwc(LayerTimelapse, { props: { jobId: "j1", api: a } });
		await flush();
		expect(wrapper.findComponent(TimelapseFrame).props("layer")).toBe(2);
		wrapper.unmount();
	});

	it("explains a job without a timelapse", async () => {
		const a = api({ timelapse: { jobId: "j1", status: "none", reason: "no snapshotUrl set", video: false, layers: [] } });
		const wrapper = mountInDwc(LayerTimelapse, { props: { jobId: "j1", layer: 1, api: a } });
		await flush();
		expect(wrapper.findComponent(TimelapseFrame).exists()).toBe(false);
		expect(wrapper.text()).toContain("plugins.QualityAssurance.timelapse.none");
	});

	it("asks again while the job is still recorded", async () => {
		vi.useFakeTimers();
		const a = api({ timelapse: { jobId: "j1", status: "capturing", video: false, layers: [] } });
		const wrapper = mountInDwc(LayerTimelapse, { props: { jobId: "j1", layer: 1, api: a } });
		await vi.advanceTimersByTimeAsync(0);
		expect(a.timelapseMeta).toHaveBeenCalledTimes(1);
		await vi.advanceTimersByTimeAsync(5100);
		expect(a.timelapseMeta).toHaveBeenCalledTimes(2);
		wrapper.unmount();
		await vi.advanceTimersByTimeAsync(10000);
		expect(a.timelapseMeta).toHaveBeenCalledTimes(2);
	});
});

describe("registration", () => {
	it("offers both views to other plugins' pages and withdraws them on unload", async () => {
		vi.doMock("../../src/QualityAssurance.vue", () => ({ default: { render: () => null } }));
		const Events = (await import("@/utils/events")).default;
		await import("../../src/index.ts");
		expect(registeredEmbeddables().map((e) => [e.id, e.pluginId, e.component])).toEqual([
			["QualityAssurance.LayerReplay", "QualityAssurance", LayerReplay],
			["QualityAssurance.LayerTimelapse", "QualityAssurance", LayerTimelapse]
		]);
		Events.emit("dwcPluginUnloaded", "QualityAssurance");
		expect(registeredEmbeddables()).toEqual([]);
	});
});
