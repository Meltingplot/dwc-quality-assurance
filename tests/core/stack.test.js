import { describe, expect, it, vi } from "vitest";

import { fitView, toScreen } from "../../src/core/replay";
import {
	drawStackLayer, drawToolpath, fetchStack, fitStack, ISO_ELEVATION, isoCamera, levelOfDetail, MIN_ELEVATION, orbit, Projection, Scratch,
	TOP_CAMERA, toolpathZ, toStackLayer, zAtLayer
} from "../../src/core/stack";

/** A 2D context that records every call and assignment */
function recorder() {
	const calls = [];
	const state = {};
	const ctx = new Proxy(state, {
		get(target, name) {
			return name in target ? target[name] : (...args) => calls.push([name, ...args]);
		},
		set(target, name, value) {
			target[name] = value;
			calls.push([`=${String(name)}`, value]);
			return true;
		}
	});
	return { ctx, calls };
}

const BOUNDS = { minX: 0, minY: 0, maxX: 100, maxY: 50, maxZ: 40 };

function layer(answer) {
	return toStackLayer({ layer: 1, z: 0.2, object: answer.start.map(() => null), ...answer });
}

describe("stack data", () => {
	it("types a layer, none without extrusion", () => {
		const l = layer({ x: [0, 10, 10], y: [0, 0, 10], bulge: [0, 0, 0], start: [0] });
		expect(Array.from(l.start)).toEqual([0, 3]);
		expect(l.x).toBeInstanceOf(Float32Array);
		expect(toStackLayer({ layer: 2, z: null, x: [], y: [], bulge: [], start: [], object: [] })).toBeNull();
	});

	it("Z of a toolpath is its last extruding segment's", () => {
		const tp = { segments: { z: [0.2, 0.2, 0.6], travel: [0, 0, 1] } };
		expect(toolpathZ(tp)).toBe(0.2);
		expect(toolpathZ(null)).toBeNull();
	});

	it("Z of a layer between the known ones", () => {
		const known = [{ layer: 1, z: 0.2 }, { layer: 4, z: 0.8 }, { layer: 7, z: 1.4 }];
		expect(zAtLayer(known, 4)).toBe(0.8);
		expect(zAtLayer(known, 5)).toBeCloseTo(1.0);
		expect(zAtLayer(known, 8)).toBeNull();
		expect(zAtLayer([], 1)).toBeNull();
	});

	it("asks the daemon from a layer on, waits while it indexes", async () => {
		const answer = { layers: [], next: null, meta: {} };
		const api = { toolpathStack: vi.fn(async () => answer) };
		expect(await fetchStack(api, "j1", 5)).toEqual({ state: "ready", answer });
		expect(api.toolpathStack).toHaveBeenCalledWith("j1", 5);
		api.toolpathStack.mockResolvedValueOnce({ state: "building" });
		expect(await fetchStack(api, "j1", 0)).toEqual({ state: "building" });
		api.toolpathStack.mockRejectedValueOnce(new Error("bad status code 409"));
		expect(await fetchStack(api, "j1", 0)).toEqual({ state: "fileGone" });
		api.toolpathStack.mockRejectedValueOnce(new Error("timeout"));
		expect(await fetchStack(api, "j1", 0)).toEqual({ state: "error", message: "timeout" });
	});
});

describe("camera", () => {
	it("from straight above it is the 2D view", () => {
		const view = fitView({ minX: 0, minY: 0, maxX: 10, maxY: 10 }, 120, 120, 10);
		const p = new Projection(TOP_CAMERA, view);
		const [x, y] = p.screen(3, 7, 25);
		const [ex, ey] = toScreen(view, 3, 7);
		expect(x).toBeCloseTo(ex, 9);
		expect(y).toBeCloseTo(ey, 9);
	});

	it("isometric: from the front left, higher is up", () => {
		const camera = isoCamera(BOUNDS);
		expect(camera.elevation).toBeCloseTo(35.264 * Math.PI / 180, 4);
		const p = new Projection(camera, { scale: 1, originX: 0, originY: 0, height: 0 });
		expect(p.plane(50, 25, 20)).toEqual([0, 0]);  // it turns about the print's centre
		// X runs to the right, Y to the left, both away from the viewer (up the screen)
		const [u0, w0] = p.plane(0, 0, 0), [ux, wx] = p.plane(100, 0, 0), [uy, wy] = p.plane(0, 50, 0);
		expect(ux).toBeGreaterThan(u0);
		expect(uy).toBeLessThan(u0);
		expect(wx).toBeGreaterThan(w0);
		expect(wy).toBeGreaterThan(w0);
		expect(p.plane(0, 0, 10)[1]).toBeGreaterThan(w0);
	});

	it("fits the print's box and orbits within limits", () => {
		const camera = isoCamera(BOUNDS);
		const p = new Projection(camera, fitStack(BOUNDS, camera, 400, 300));
		for (const x of [0, 100]) {
			for (const y of [0, 50]) {
				for (const z of [0, 40]) {
					const [sx, sy] = p.screen(x, y, z);
					expect(sx).toBeGreaterThanOrEqual(15.99);
					expect(sx).toBeLessThanOrEqual(384.01);
					expect(sy).toBeGreaterThanOrEqual(15.99);
					expect(sy).toBeLessThanOrEqual(284.01);
				}
			}
		}
		expect(orbit(camera, 0, -1000).elevation).toBe(MIN_ELEVATION);
		expect(orbit(camera, 0, 1000).elevation).toBe(Math.PI / 2);
		expect(orbit(camera, 100, 0).azimuth).toBeCloseTo(camera.azimuth + 1);
	});
});

describe("drawing a stack layer", () => {
	// a line from (0, 0) to (10, 0), then a counter-clockwise half circle to (20, 0) through (15, −5)
	const HALF = layer({ x: [0, 10, 20], y: [0, 0, 0], bulge: [0, 0, 1], start: [0] });

	function arcEnds(calls) {
		return calls.filter((c) => c[0] === "ellipse").map(([, cx, cy, rx, ry, , from, to]) => ({
			from: [cx + rx * Math.cos(from), cy + ry * Math.sin(from)],
			to: [cx + rx * Math.cos(to), cy + ry * Math.sin(to)]
		}));
	}

	for (const [name, camera] of [["from above", TOP_CAMERA], ["isometric", { ...isoCamera(BOUNDS), azimuth: 0.7 }]]) {
		it(`an arc joins its vertices on the canvas (${name})`, () => {
			const p = new Projection(camera, { scale: 7, originX: -40, originY: -30, height: 300 });
			const { ctx, calls } = recorder();
			drawStackLayer(ctx, HALF, p, [], new Scratch());
			const ends = arcEnds(calls);
			expect(ends).toHaveLength(8);  // half a turn in eighths of a half turn, each lit by its own normal
			const close = (a, b) => {
				expect(a[0]).toBeCloseTo(b[0], 3);
				expect(a[1]).toBeCloseTo(b[1], 3);
			};
			// the pieces follow each other from the first vertex to the last, through the arc's middle
			ends.sort((a, b) => a.from[0] - b.from[0] || a.from[1] - b.from[1]);
			const chain = [p.screen(10, 0, 0.2)];
			for (let k = 0; k < 8; k++) {
				const next = ends.find((e) => Math.hypot(e.from[0] - chain[k][0], e.from[1] - chain[k][1]) < 1e-3);
				expect(next).toBeDefined();
				chain.push(next.to);
			}
			close(chain[4], p.screen(15, -5, 0.2));
			close(chain[8], p.screen(20, 0, 0.2));
			// every piece starts where the pen was moved to
			calls.forEach((c, i) => {
				if (c[0] === "ellipse") {
					expect(calls[i - 1][0]).toBe("moveTo");
				}
			});
			const line = calls.filter((c) => c[0] === "lineTo");
			expect(line).toHaveLength(1);
			close(line[0].slice(1), p.screen(10, 0, 0.2));
		});
	}

	it("leaves hidden objects out and strokes each shade once", () => {
		const two = toStackLayer({ layer: 3, z: 0.6, x: [0, 10, 0, 0], y: [0, 0, 0, 10], bulge: [0, 0, 0, 0], start: [0, 2], object: [1, 2] });
		const p = new Projection(TOP_CAMERA, { scale: 1, originX: 0, originY: 0, height: 100 });
		const { ctx, calls } = recorder();
		drawStackLayer(ctx, two, p, [2], new Scratch());
		expect(calls.filter((c) => c[0] === "lineTo")).toEqual([["lineTo", 10, 100]]);
		expect(calls.filter((c) => c[0] === "stroke")).toHaveLength(1);
	});

	it("lighter while the view moves: every n-th layer, the top one kept", () => {
		const layers = [1, 2, 3, 4, 5].map((n) => ({ layer: n, x: new Float32Array(10) }));
		expect(levelOfDetail(layers, 100)).toBe(layers);
		expect(levelOfDetail(layers, 20).map((l) => l.layer)).toEqual([1, 4, 5]);
	});
});

describe("drawing a toolpath", () => {
	const TP = {
		layer: 2,
		segments: {
			x0: [0, 10, 10, 20], y0: [0, 0, 10, 20], x1: [10, 10, 20, 30], y1: [0, 10, 20, 30], z: [0.4, 0.4, 0.4, 0.4],
			e: [1, 1, 0, 1], flow: [2, 2, 0, 4], object: [0, 0, null, 1], type: [0, 0, 0, 0], travel: [0, 0, 1, 0]
		}
	};
	const p = new Projection(TOP_CAMERA, { scale: 1, originX: 0, originY: 0, height: 100 });

	it("one stroke per flow colour, joined where segments meet", () => {
		const { ctx, calls } = recorder();
		drawToolpath(ctx, TP, p, [2, 4], [], false, new Scratch());
		const strokes = calls.filter((c) => c[0] === "=strokeStyle").map((c) => c[1]);
		expect(strokes).toEqual(["rgb(68,1,84)", "rgb(253,231,37)"]);
		// the first two segments are one polyline
		const first = calls.slice(0, calls.findIndex((c) => c[0] === "stroke"));
		expect(first.filter((c) => c[0] === "moveTo")).toHaveLength(1);
		expect(first.filter((c) => c[0] === "lineTo")).toHaveLength(2);
	});

	it("travel on top when shown, hidden objects left out", () => {
		const { ctx, calls } = recorder();
		drawToolpath(ctx, TP, p, [2, 4], [1], true, new Scratch());
		const strokes = calls.filter((c) => c[0] === "=strokeStyle").map((c) => c[1]);
		expect(strokes).toEqual(["rgb(68,1,84)", "rgba(128,128,128,0.35)"]);
	});
});

describe("isometric elevation", () => {
	it("is the angle of a cube's space diagonal", () => {
		expect(Math.tan(ISO_ELEVATION)).toBeCloseTo(Math.SQRT1_2);
	});
});
