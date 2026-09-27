import { afterEach, describe, expect, it, vi } from "vitest";

import { forgetVideo, frameForLayer, isPending, layerAtTime, layersWithFrames, seekTime, videoUrl } from "../../src/core/timelapse";

// layer 2 skipped (interval), layer 4 failed, layer 5 taken twice (daemon restart)
const META = {
	jobId: "j1", status: "done", fps: 10, frames: 4, video: true,
	layers: [
		{ layer: 1, frame: 0, ts: 1 }, { layer: 2, frame: null, ts: 2, reason: "interval" }, { layer: 3, frame: 1, ts: 3 },
		{ layer: 4, frame: null, ts: 4, reason: "snapshot: HTTP 503" }, { layer: 5, frame: 2, ts: 5 }, { layer: 5, frame: 3, ts: 6 }
	]
};

afterEach(() => {
	forgetVideo();
	vi.restoreAllMocks();
});

describe("timelapse index", () => {
	it("finds the frame of a layer, else the latest earlier one", () => {
		expect(frameForLayer(META, 1)).toEqual({ layer: 1, frame: 0 });
		expect(frameForLayer(META, 2)).toEqual({ layer: 1, frame: 0 });
		expect(frameForLayer(META, 4)).toEqual({ layer: 3, frame: 1 });
		expect(frameForLayer(META, 5)).toEqual({ layer: 5, frame: 3 });
		expect(frameForLayer(META, 0)).toBeNull();
		expect(frameForLayer(null, 1)).toBeNull();
		expect(layersWithFrames(META)).toEqual([1, 3, 5]);
	});

	it("seeks into the middle of a frame and maps a time back to its layer", () => {
		expect(seekTime(0, 10)).toBeCloseTo(0.05);
		expect(seekTime(3, 10)).toBeCloseTo(0.35);
		for (const frame of [0, 1, 2, 3]) {
			const layer = layerAtTime(META, seekTime(frame, 10));
			expect(frameForLayer(META, layer).frame === frame || (layer === 5 && frame === 2)).toBe(true);
		}
		expect(layerAtTime(META, 0.1)).toBe(3);   // frame 1 starts at 0.1 s exactly
		expect(layerAtTime({ ...META, fps: null }, 1)).toBeNull();
	});

	it("knows which states still change", () => {
		expect(["capturing", "queued", "encoding"].map((status) => isPending({ status }))).toEqual([true, true, true]);
		expect(["done", "failed", "pruned", "none"].map((status) => isPending({ status }))).toEqual([false, false, false, false]);
	});
});

describe("video cache", () => {
	it("downloads a job's video once and releases the previous job's", async () => {
		let n = 0;
		vi.spyOn(URL, "createObjectURL").mockImplementation(() => `blob:${++n}`);
		const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
		const api = { timelapseVideo: vi.fn(async () => new Blob(["x"])) };
		expect(await videoUrl(api, "a")).toBe("blob:1");
		expect(await videoUrl(api, "a")).toBe("blob:1");
		expect(api.timelapseVideo).toHaveBeenCalledTimes(1);
		expect(await videoUrl(api, "b")).toBe("blob:2");
		await Promise.resolve();
		expect(revoke).toHaveBeenCalledWith("blob:1");
	});

	it("tries again after a failed download", async () => {
		const api = { timelapseVideo: vi.fn(async () => { throw new Error("timeout"); }) };
		await expect(videoUrl(api, "a")).rejects.toThrow("timeout");
		await expect(videoUrl(api, "a")).rejects.toThrow("timeout");
		expect(api.timelapseVideo).toHaveBeenCalledTimes(2);
	});
});
