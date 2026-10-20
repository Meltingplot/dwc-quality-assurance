import { describe, expect, it, vi } from "vitest";

import {
	diffRange, divergingColor, executedPoints, fetchToolpathOm, fitView, flowColouring, flowDiff, flowRange, frameAt, machineToUser, omCoverage,
	pan, rampColor, segmentFusedFlow, segmentOmFlow, toolOffsets, toolpathBounds, toScreen, toWorld, zoomAt
} from "../../src/core/replay";

const TP = {
	layer: 1,
	segments: {
		x0: [0, 10, 10], y0: [0, 0, 10], x1: [10, 10, 50], y1: [0, 10, 50], z: [0.2, 0.2, 0.2],
		e: [1, 1, 0], flow: [2, 4, 0], object: [0, 0, null], type: [0, 0, 0], travel: [0, 0, 1], pos: [100, 130, 160]
	},
	types: [null], objects: { 0: "cube" }, meta: { numLayers: 2, source: "comments", objects: {}, filamentDiameter: 1.75 }
};

describe("replay geometry", () => {
	it("bounds with and without travel", () => {
		expect(toolpathBounds(TP)).toEqual({ minX: 0, minY: 0, maxX: 50, maxY: 50 });
		expect(toolpathBounds(TP, false)).toEqual({ minX: 0, minY: 0, maxX: 10, maxY: 10 });
	});

	it("fits, maps back and forth, zooms around the cursor and pans", () => {
		const view = fitView({ minX: 0, minY: 0, maxX: 10, maxY: 10 }, 120, 120, 10);
		expect(view.scale).toBe(10);
		expect(toScreen(view, 0, 0)).toEqual([10, 110]);  // y flipped
		expect(toWorld(view, 10, 110)).toEqual([0, 0]);
		const zoomed = zoomAt(view, 2, 60, 60);
		expect(toWorld(zoomed, 60, 60)).toEqual(toWorld(view, 60, 60));
		expect(zoomed.scale).toBe(20);
		const moved = pan(view, 10, 0);
		expect(toScreen(moved, 0, 0)[0]).toBeCloseTo(20);
	});

	it("colours and flow range", () => {
		expect(rampColor(0)).toBe("rgb(68,1,84)");
		expect(rampColor(1)).toBe("rgb(253,231,37)");
		expect(rampColor(Number.NaN)).toBe("rgb(68,1,84)");
		expect(flowRange(TP)).toEqual([2, 4]);
	});
});

describe("coordinates", () => {
	it("machine to user: minus workplace, plus tool offset (wiki G10)", () => {
		expect(machineToUser({ x: 110, y: 50 }, { X: 10, Y: 0 }, { X: -2, Y: 3 })).toEqual({ x: 98, y: 53 });
		expect(machineToUser({ x: 5, y: 5 }, null, null)).toEqual({ x: 5, y: 5 });
	});

	it("tool offsets by axis letter from the context", () => {
		const context = { axes: [{ letter: "X" }, { letter: "Y" }, { letter: "Z" }], tools: [{ number: 1, offsets: [-2, 3, 0] }] };
		expect(toolOffsets(context, 1)).toEqual({ X: -2, Y: 3, Z: 0 });
		expect(toolOffsets(context, 0)).toEqual({});
		expect(toolOffsets(null, 1)).toEqual({});
	});

	it("executed points take the position valid at the sample time", () => {
		const channels = {
			"axis.X.machinePosition": [[100, 1], [300, 3]],
			"axis.Y.machinePosition": [[100, 10]],
			"move.currentMove.extrusionRate": [[50, 2], [200, 2], [300, 0], [400, 1]]
		};
		const points = executedPoints(channels, 2);
		expect(points).toEqual([{ ts: 200, x: 1, y: 10, flow: 4 }, { ts: 400, x: 3, y: 10, flow: 2 }]);
		// positions from before the layer still place the first points of it
		expect(executedPoints(channels, 2, 250)).toEqual([{ ts: 400, x: 3, y: 10, flow: 2 }]);
	});

	it("workplace and tool in force at a time come from the nearest event", () => {
		const events = [
			{ ts_ms: 100, offsets: { X: 10 }, tool: -1 },
			{ ts_ms: 200, offsets: null, tool: 0 },
			{ ts_ms: 300, offsets: { X: 20 }, tool: 1 }
		];
		expect(frameAt(events, 150)).toEqual({ offsets: { X: 10 }, tool: 0 });  // no tool selected yet: the next event's
		expect(frameAt(events, 250)).toEqual({ offsets: { X: 10 }, tool: 0 });
		expect(frameAt(events, 300)).toEqual({ offsets: { X: 20 }, tool: 1 });
		expect(frameAt(events, 50)).toEqual({ offsets: { X: 10 }, tool: 0 });
		expect(frameAt([], 50)).toEqual({ offsets: null, tool: null });
	});
});

describe("flow RRF planned (object model)", () => {
	// a 30 mm move in three chords (an arc: one line), a move without a sample, travel with one
	const ARC = {
		...TP,
		segments: {
			x0: [0, 10, 20, 30, 40], y0: [0, 0, 0, 0, 0], x1: [10, 20, 30, 40, 50], y1: [0, 0, 0, 0, 0], z: [0.2, 0.2, 0.2, 0.2, 0.2],
			e: [1, 1, 1, 1, 0], flow: [4, 4, 4, 2, 0], object: [0, 0, 0, 0, null], type: [0, 0, 0, 0, 0], travel: [0, 0, 0, 0, 1],
			pos: [100, 100, 100, 140, 180]
		}
	};
	const OM = { layer: 1, from: 0, to: 1, samples: 3, seen: 5, moves: { pos: [100, 180, 999], flow: [3, 7, 1], n: [1, 1, 1] },
		plan: { pos: [100, 140], flow: [3.5, 1.5] }, correction: 0.9 };

	it("joins the moves to the segments by their line's file position", () => {
		const om = segmentOmFlow(ARC, OM);
		expect(Array.from(om.slice(0, 3))).toEqual([3, 3, 3]);  // every chord of the line
		expect(om[3]).toBeNaN();  // no sample
		expect(om[4]).toBeNaN();  // travel
		expect(Array.from(flowDiff(ARC, om).slice(0, 3))).toEqual([-1, -1, -1]);
		expect(omCoverage(ARC, om)).toBeCloseTo(0.75);
		expect(omCoverage({ ...ARC, segments: { ...ARC.segments, travel: [1, 1, 1, 1, 1] } }, om)).toBeNull();
	});

	it("fuses: the sample where there is one, else the plan times the layer's correction", () => {
		const fused = segmentFusedFlow(ARC, OM);
		expect(Array.from(fused.slice(0, 4))).toEqual([3, 3, 3, 1.5 * 0.9]);
		expect(fused[4]).toBeNaN();  // travel
		expect(segmentFusedFlow(ARC, { ...OM, correction: null })[3]).toBe(1.5);
		expect(flowColouring(ARC, "fused", fused).range).toEqual(flowRange(ARC));
	});

	it("colours: the G-code's scale for both flows, a symmetric one for the difference", () => {
		const om = segmentOmFlow(ARC, OM);
		expect(flowColouring(ARC, "gcode", om).values).toBe(ARC.segments.flow);
		const planned = flowColouring(ARC, "om", om);
		expect(planned.range).toEqual(flowRange(ARC));
		expect(planned.values).toBe(om);
		const diff = flowColouring(ARC, "diff", om);
		expect(diff.range).toEqual([-1, 1]);
		expect(diff.color(0.5)).toBe("rgb(143,143,139)");  // no difference: neutral grey
		// not loaded yet: no value anywhere
		expect(Array.from(flowColouring(ARC, "om", null).values).every(Number.isNaN)).toBe(true);
	});

	it("diverging colours and range", () => {
		expect(divergingColor(0)).toBe("rgb(42,120,214)");
		expect(divergingColor(1)).toBe("rgb(227,73,72)");
		expect(diffRange([Number.NaN, -0.5, 0.1, 0.2])).toEqual([-0.5, 0.5]);
		expect(diffRange([0, Number.NaN])).toEqual([-0.01, 0.01]);
	});

	it("fetches the layer's moves: ready, building, none, file gone, error", async () => {
		const api = (answer) => ({ toolpathOm: vi.fn(answer) });
		expect(await fetchToolpathOm(api(async () => OM), "j", 1)).toEqual({ state: "ready", answer: OM });
		expect(await fetchToolpathOm(api(async () => ({ state: "building" })), "j", 1)).toEqual({ state: "building" });
		const fail = (name, message) => api(async () => { throw Object.assign(new Error(message), { name }); });
		expect(await fetchToolpathOm(fail("FileNotFoundError", "no journal"), "j", 1)).toEqual({ state: "none" });
		expect(await fetchToolpathOm(fail("OperationFailedError", "bad status code 409"), "j", 1)).toEqual({ state: "fileGone" });
		expect(await fetchToolpathOm(fail("OperationFailedError", "timeout"), "j", 1)).toEqual({ state: "error", message: "timeout" });
	});
});
