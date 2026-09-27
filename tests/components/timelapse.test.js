import { mountInDwc } from "dwc-plugin-test-kit";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { forgetVideo } from "../../src/core/timelapse";
import { expectNoVueWarnings } from "../helpers.js";

const TimelapseViewer = (await import("../../src/components/TimelapseViewer.vue")).default;
const TimelapseFrame = (await import("../../src/components/TimelapseFrame.vue")).default;

const JOB = { id: "j1", file: "0:/gcodes/a.gcode", numLayers: 3 };
const LAYERS = [{ layer: 1, frame: 0, ts: 1 }, { layer: 2, frame: 1, ts: 2 }, { layer: 3, frame: 2, ts: 3 }];

let warn;
let urls;
beforeEach(() => {
	warn = vi.spyOn(console, "warn").mockImplementation(() => {});
	urls = 0;
	vi.spyOn(URL, "createObjectURL").mockImplementation(() => `blob:qa-${++urls}`);
	vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
});
afterEach(() => {
	forgetVideo();
	vi.restoreAllMocks();
	vi.useRealTimers();
});

function api(meta) {
	return {
		timelapseMeta: vi.fn(async () => (typeof meta === "function" ? meta() : meta)),
		timelapseFrame: vi.fn(async () => new Blob(["jpeg"])),
		timelapseVideo: vi.fn(async () => new Blob(["mp4"]))
	};
}

const flush = async () => {
	for (let i = 0; i < 5; i++) {
		await new Promise((r) => setTimeout(r, 0));
	}
};

describe("TimelapseViewer", () => {
	it("says why nothing was recorded", async () => {
		const a = api({ jobId: "j1", status: "none", reason: "no snapshotUrl set", video: false, layers: [] });
		const wrapper = mountInDwc(TimelapseViewer, { props: { api: a, job: JOB } });
		await flush();
		expect(wrapper.text()).toContain("plugins.QualityAssurance.timelapse.states.none");
		expect(wrapper.text()).toContain("plugins.QualityAssurance.timelapse.none");
		expect(a.timelapseFrame).not.toHaveBeenCalled();
		expectNoVueWarnings(warn);
	});

	it("shows the captured frames while recording and asks again every 5 s", async () => {
		vi.useFakeTimers();
		const a = api({ jobId: "j1", status: "capturing", fps: 30, frames: 2, video: false, layers: LAYERS.slice(0, 2) });
		const wrapper = mountInDwc(TimelapseViewer, { props: { api: a, job: JOB } });
		await vi.advanceTimersByTimeAsync(0);
		expect(a.timelapseFrame).toHaveBeenLastCalledWith("j1", 1);
		expect(wrapper.find("img").attributes("src")).toBe("blob:qa-1");
		wrapper.vm.index = 1;
		await vi.advanceTimersByTimeAsync(0);
		expect(a.timelapseFrame).toHaveBeenLastCalledWith("j1", 2);
		expect(a.timelapseVideo).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(5000);
		expect(a.timelapseMeta).toHaveBeenCalledTimes(2);
		wrapper.unmount();
		await vi.advanceTimersByTimeAsync(10000);
		expect(a.timelapseMeta).toHaveBeenCalledTimes(2);  // no polling after unmount
		expectNoVueWarnings(warn);
	});

	it("plays the video, seeks per layer, follows the playback and offers the download", async () => {
		const a = api({ jobId: "j1", status: "done", fps: 10, frames: 3, sizeBytes: 2048, video: true, layers: LAYERS });
		const wrapper = mountInDwc(TimelapseViewer, { props: { api: a, job: JOB } });
		await flush();
		const video = wrapper.find("video");
		expect(video.attributes("src")).toBe("blob:qa-1");
		expect(wrapper.find("a[download]").attributes("href")).toBe("blob:qa-1");
		expect(wrapper.find("a[download]").attributes("download")).toBe("qa-j1-timelapse.mp4");
		wrapper.vm.index = 2;
		wrapper.vm.seekTo();
		expect(video.element.currentTime).toBeCloseTo(0.25);  // frame 2 of 10 fps: its middle
		video.element.currentTime = 0.12;
		await video.trigger("timeupdate");
		expect(wrapper.vm.layer).toBe(2);
		expect(a.timelapseFrame).not.toHaveBeenCalled();
		expect(a.timelapseMeta).toHaveBeenCalledTimes(1);  // final: no polling
		expectNoVueWarnings(warn);
	});

	it("shows the error and the frames it kept when encoding failed", async () => {
		const a = api({ jobId: "j1", status: "failed", error: "ffmpeg exit 1", fps: 30, frames: 3, video: false, layers: LAYERS });
		const wrapper = mountInDwc(TimelapseViewer, { props: { api: a, job: JOB } });
		await flush();
		expect(wrapper.text()).toContain("plugins.QualityAssurance.timelapse.failed");
		expect(a.timelapseFrame).toHaveBeenCalledWith("j1", 1);
		expectNoVueWarnings(warn);
	});
});

describe("TimelapseFrame", () => {
	it("falls back to the latest earlier frame and says so", async () => {
		const meta = { jobId: "j1", status: "capturing", fps: 30, video: false,
			layers: [{ layer: 1, frame: 0, ts: 1 }, { layer: 2, frame: null, ts: 2, reason: "interval" }] };
		const a = api(meta);
		const wrapper = mountInDwc(TimelapseFrame, { props: { api: a, jobId: "j1", meta, layer: 2 } });
		await flush();
		expect(a.timelapseFrame).toHaveBeenCalledWith("j1", 1);
		expect(wrapper.text()).toContain("plugins.QualityAssurance.timelapse.frameOfEarlier");
		await wrapper.setProps({ layer: 1 });
		await flush();
		expect(a.timelapseFrame).toHaveBeenCalledTimes(1);  // same frame: nothing to load
		expect(wrapper.text()).toContain("plugins.QualityAssurance.timelapse.frameOf");
		expectNoVueWarnings(warn);
	});

	it("seeks the shared video once it exists", async () => {
		const meta = { jobId: "j1", status: "done", fps: 10, video: true, layers: LAYERS };
		const a = api(meta);
		const wrapper = mountInDwc(TimelapseFrame, { props: { api: a, jobId: "j1", meta, layer: 3 } });
		await flush();
		const video = wrapper.find("video");
		await video.trigger("loadedmetadata");
		expect(video.element.currentTime).toBeCloseTo(0.25);
		await wrapper.setProps({ layer: 2 });
		await flush();
		expect(video.element.currentTime).toBeCloseTo(0.15);
		expect(a.timelapseVideo).toHaveBeenCalledTimes(1);
		expect(a.timelapseFrame).not.toHaveBeenCalled();
		expectNoVueWarnings(warn);
	});
});
