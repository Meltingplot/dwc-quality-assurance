/**
 * The replay's 3D stack (PLAN.md §5.8): the layers below the one shown, drawn in an orthographic view
 * that orbits the print, so the object builds up layer by layer and events line up across layers.
 * The daemon sends the stack reduced (``job/toolpath/stack``: paths of lines and arcs within about a
 * pixel of a fitted view, sparse infill left out, every n-th layer of a tall print); the layer shown and
 * the events keep full resolution. The 2D replay is the same drawing seen from straight above
 * (``TOP_CAMERA``). Pure functions on a 2D context, no DOM.
 */
import { statusOf, type PrintBounds, type QaApi, type StackLayerAnswer, type ToolpathAnswer, type ToolpathStackAnswer } from "./api";
import { fitView, rampColor, type Bounds, type View } from "./replay";

/** One layer of the stack, typed arrays for drawing */
export interface StackLayer {
	layer: number;
	z: number;
	x: Float32Array;
	y: Float32Array;
	bulge: Float32Array;
	/** First vertex of each path, then the vertex count */
	start: Int32Array;
	object: Array<number | null>;
}

/** A layer as drawn, or null when it extrudes nothing */
export function toStackLayer(answer: StackLayerAnswer): StackLayer | null {
	if (answer.z === null) {
		return null;
	}
	return {
		layer: answer.layer,
		z: answer.z,
		x: Float32Array.from(answer.x),
		y: Float32Array.from(answer.y),
		bulge: Float32Array.from(answer.bulge),
		start: Int32Array.from([...answer.start, answer.x.length]),
		object: answer.object
	};
}

export type StackResult =
	| { state: "ready"; answer: ToolpathStackAnswer }
	| { state: "building" }
	| { state: "fileGone" }
	| { state: "error"; message: string };

export async function fetchStack(api: QaApi, jobId: string, from: number): Promise<StackResult> {
	try {
		const answer = await api.toolpathStack(jobId, from);
		return "state" in answer ? { state: "building" } : { state: "ready", answer };
	} catch (e) {
		if (statusOf(e) === 409) {
			return { state: "fileGone" };
		}
		return { state: "error", message: e instanceof Error ? e.message : String(e) };
	}
}

/** Z of a layer's toolpath: the Z of its last extruding segment, as the daemon gives a stack layer's */
export function toolpathZ(tp: ToolpathAnswer | null): number | null {
	const s = tp?.segments;
	for (let i = (s?.z.length ?? 0) - 1; i >= 0; i--) {
		if (!s!.travel[i]) {
			return s!.z[i];
		}
	}
	return null;
}

/**
 * Z of ``layer`` from the layers whose Z is known (sorted by layer): exact, or between the nearest known
 * layers below and above (a stack of every n-th layer), null outside them
 */
export function zAtLayer(known: Array<{ layer: number; z: number }>, layer: number): number | null {
	let lo = 0, hi = known.length;
	while (lo < hi) {
		const mid = (lo + hi) >> 1;
		if (known[mid].layer < layer) {
			lo = mid + 1;
		} else {
			hi = mid;
		}
	}
	if (lo < known.length && known[lo].layer === layer) {
		return known[lo].z;
	}
	if (lo === 0 || lo === known.length) {
		return null;
	}
	const a = known[lo - 1], b = known[lo];
	return a.z + (b.z - a.z) * (layer - a.layer) / (b.layer - a.layer);
}

export interface Camera {
	/** Turn of the print about Z (radians); 0 looks at its front (−Y) */
	azimuth: number;
	/** Angle of the view above the bed (radians); π/2 looks straight down */
	elevation: number;
	/** The point the view turns about */
	cx: number;
	cy: number;
	cz: number;
}

/** Isometric: from the front left, 35.26° above the bed (a cube's corner points at the viewer) */
export const ISO_AZIMUTH = Math.PI / 4;
export const ISO_ELEVATION = Math.atan(Math.SQRT1_2);
export const MIN_ELEVATION = Math.PI / 36;
/** Straight down onto the bed at G-code coordinates: the 2D replay */
export const TOP_CAMERA: Camera = { azimuth: 0, elevation: Math.PI / 2, cx: 0, cy: 0, cz: 0 };

export function isoCamera(bounds: PrintBounds): Camera {
	return {
		azimuth: ISO_AZIMUTH, elevation: ISO_ELEVATION,
		cx: (bounds.minX + bounds.maxX) / 2, cy: (bounds.minY + bounds.maxY) / 2, cz: bounds.maxZ / 2
	};
}

/** Turn and tilt by a pointer drag of (dx, dy) px: the print follows the pointer */
export function orbit(camera: Camera, dx: number, dy: number): Camera {
	return {
		...camera,
		azimuth: camera.azimuth + dx * 0.01,
		elevation: Math.max(MIN_ELEVATION, Math.min(Math.PI / 2, camera.elevation + dy * 0.01))
	};
}

/** Camera and view in one: world (x, y, z) to canvas px */
export class Projection {
	readonly ca: number;
	readonly sa: number;
	readonly se: number;
	readonly ce: number;
	/** Where the light comes from (world XY, fixed to the camera: from the viewer's front left) */
	readonly lx: number;
	readonly ly: number;

	constructor(readonly camera: Camera, readonly view: View) {
		this.ca = Math.cos(camera.azimuth);
		this.sa = Math.sin(camera.azimuth);
		this.se = Math.sin(camera.elevation);
		this.ce = Math.cos(camera.elevation);
		// −(0.5 × the camera's right axis (ca, −sa) + its forward axis (sa, ca)), normalised
		const lx = -(0.5 * this.ca + this.sa), ly = -(-0.5 * this.sa + this.ca);
		const length = Math.hypot(lx, ly);
		this.lx = lx / length;
		this.ly = ly / length;
	}

	/** Horizontal and vertical position in the view plane (mm) */
	plane(x: number, y: number, z: number): [number, number] {
		const dx = x - this.camera.cx, dy = y - this.camera.cy;
		return [dx * this.ca - dy * this.sa, (dx * this.sa + dy * this.ca) * this.se + (z - this.camera.cz) * this.ce];
	}

	screen(x: number, y: number, z: number): [number, number] {
		const [u, w] = this.plane(x, y, z);
		const v = this.view;
		return [(u - v.originX) * v.scale, v.height - (w - v.originY) * v.scale];
	}
}

/** A view that fits the box of ``bounds`` (from the bed up) as ``camera`` sees it */
export function fitStack(bounds: PrintBounds, camera: Camera, width: number, height: number, margin = 16): View {
	const probe = new Projection(camera, { scale: 1, originX: 0, originY: 0, height: 0 });
	const box: Bounds = { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
	for (const x of [bounds.minX, bounds.maxX]) {
		for (const y of [bounds.minY, bounds.maxY]) {
			for (const z of [0, bounds.maxZ]) {
				const [u, w] = probe.plane(x, y, z);
				box.minX = Math.min(box.minX, u);
				box.maxX = Math.max(box.maxX, u);
				box.minY = Math.min(box.minY, w);
				box.maxY = Math.max(box.maxY, w);
			}
		}
	}
	return fitView(box, width, height, margin);
}

const SHADES = 8;
/** Slate grey, lit from 40 % to full: readable on DWC's light and dark background */
const SHADE_COLORS = Array.from({ length: SHADES }, (_, i) => {
	const f = 0.4 + 0.6 * i / (SHADES - 1);
	return `rgb(${Math.round(150 * f)},${Math.round(164 * f)},${Math.round(182 * f)})`;
});
const ARC_PIECE = Math.PI / 8;
const SKIP = 255;

/** Shade of a wall whose horizontal normal is (nx, ny), unit length: facing the light bright, edge-on dark */
function shade(p: Projection, nx: number, ny: number): number {
	return Math.min(SHADES - 1, Math.round(Math.abs(nx * p.lx + ny * p.ly) * (SHADES - 1)));
}

/** Width of an extrusion line on the canvas */
export function lineWidth(view: View): number {
	return Math.max(1, Math.min(4, view.scale * 0.4));
}

/** Growing scratch arrays, so drawing a layer allocates nothing */
export class Scratch {
	sx = new Float32Array(0);
	sy = new Float32Array(0);
	bucket = new Uint8Array(0);

	ensure(n: number) {
		if (this.sx.length < n) {
			const size = Math.max(n, this.sx.length * 2);
			this.sx = new Float32Array(size);
			this.sy = new Float32Array(size);
			this.bucket = new Uint8Array(size);
		}
	}
}

interface ArcPiece {
	cx: number;
	cy: number;
	rx: number;
	ry: number;
	from: number;
	to: number;
	anticlockwise: boolean;
}

/**
 * One stack layer in grey shades: its walls lit from the viewer's front left, so the drawn layers read
 * as a solid. Layers must be drawn bottom up: seen from above, a higher point on the same line of sight
 * is always the nearer one, so the later layer covers correctly.
 */
export function drawStackLayer(ctx: CanvasRenderingContext2D, layer: StackLayer, p: Projection, hidden: ReadonlyArray<number>, scratch: Scratch) {
	const n = layer.x.length;
	scratch.ensure(n);
	const { sx, sy, bucket } = scratch;
	const { ca, sa, se, ce } = p;
	const { cx, cy, cz } = p.camera;
	const { scale, originX, originY, height } = p.view;
	const wz = (layer.z - cz) * ce;
	for (let i = 0; i < n; i++) {
		const dx = layer.x[i] - cx, dy = layer.y[i] - cy;
		sx[i] = (dx * ca - dy * sa - originX) * scale;
		sy[i] = height - ((dx * sa + dy * ca) * se + wz - originY) * scale;
	}
	const arcs: Array<Array<ArcPiece>> = Array.from({ length: SHADES }, () => []);
	for (let k = 0; k + 1 < layer.start.length; k++) {
		const first = layer.start[k], end = layer.start[k + 1];
		const obj = layer.object[k];
		const skip = obj !== null && hidden.includes(obj);
		bucket[first] = SKIP;
		for (let i = first + 1; i < end; i++) {
			if (skip) {
				bucket[i] = SKIP;
				continue;
			}
			const px = layer.x[i - 1], py = layer.y[i - 1], qx = layer.x[i], qy = layer.y[i];
			const chord = Math.hypot(qx - px, qy - py);
			const b = layer.bulge[i];
			if (Math.abs(b) < 1e-4 || chord === 0) {
				bucket[i] = chord === 0 ? SHADES - 1 : shade(p, -(qy - py) / chord, (qx - px) / chord);
				continue;
			}
			bucket[i] = SKIP;
			// centre off the chord's middle, to its left for a counter-clockwise arc
			const h = chord / 2 * (1 - b * b) / (2 * b);
			const ccx = (px + qx) / 2 - (qy - py) / chord * h, ccy = (py + qy) / 2 + (qx - px) / chord * h;
			const radius = Math.hypot(px - ccx, py - ccy);
			const sweep = 4 * Math.atan(b);
			const t0 = Math.atan2(py - ccy, px - ccx);
			const [scx, scy] = p.screen(ccx, ccy, layer.z);
			const pieces = Math.max(1, Math.ceil(Math.abs(sweep) / ARC_PIECE));
			for (let j = 0; j < pieces; j++) {
				const ta = t0 + sweep * j / pieces, tb = t0 + sweep * (j + 1) / pieces, tm = (ta + tb) / 2;
				// an arc's wall faces along its radius
				arcs[shade(p, Math.cos(tm), Math.sin(tm))].push({
					cx: scx, cy: scy, rx: radius * scale, ry: radius * se * scale,
					from: -(ta + p.camera.azimuth), to: -(tb + p.camera.azimuth), anticlockwise: sweep > 0
				});
			}
		}
	}
	for (let s = 0; s < SHADES; s++) {
		let used = false;
		ctx.beginPath();
		let pen = -1;
		for (let i = 0; i < n; i++) {
			if (bucket[i] !== s) {
				continue;
			}
			if (pen !== i - 1) {
				ctx.moveTo(sx[i - 1], sy[i - 1]);
			}
			ctx.lineTo(sx[i], sy[i]);
			pen = i;
			used = true;
		}
		for (const a of arcs[s]) {
			ctx.moveTo(a.cx + a.rx * Math.cos(a.from), a.cy + a.ry * Math.sin(a.from));
			ctx.ellipse(a.cx, a.cy, a.rx, a.ry, 0, a.from, a.to, a.anticlockwise);
			used = true;
		}
		if (used) {
			ctx.strokeStyle = SHADE_COLORS[s];
			ctx.stroke();
		}
	}
}

/** Stack layers to draw while the view moves: every n-th, about ``budget`` vertices in all */
export function levelOfDetail(layers: Array<StackLayer>, budget: number): Array<StackLayer> {
	const total = layers.reduce((sum, l) => sum + l.x.length, 0);
	const step = Math.max(1, Math.ceil(total / budget));
	return step === 1 ? layers : layers.filter((_, i) => i % step === 0 || i === layers.length - 1);
}

const FLOW_COLORS = 32;
const FLOW_RAMP = Array.from({ length: FLOW_COLORS }, (_, i) => rampColor(i / (FLOW_COLORS - 1)));

/**
 * A layer's full toolpath, coloured by commanded flow within ``range``, each segment at its own Z; travel
 * thin and grey on top when shown. Batched by colour, so a large layer costs a few strokes.
 */
export function drawToolpath(ctx: CanvasRenderingContext2D, tp: ToolpathAnswer, p: Projection, range: [number, number],
	hidden: ReadonlyArray<number>, showTravel: boolean, scratch: Scratch) {
	const s = tp.segments;
	const n = s.x0.length;
	scratch.ensure(n);
	const colour = scratch.bucket;
	const counts = new Int32Array(FLOW_COLORS + 1);
	const [lo, hi] = range;
	for (let i = 0; i < n; i++) {
		const obj = s.object[i];
		if (obj !== null && hidden.includes(obj)) {
			colour[i] = SKIP;
		} else if (s.travel[i]) {
			colour[i] = showTravel ? FLOW_COLORS : SKIP;
		} else {
			const u = Math.max(0, Math.min(1, (s.flow[i] - lo) / (hi - lo) || 0));
			colour[i] = Math.round(u * (FLOW_COLORS - 1));
		}
		if (colour[i] !== SKIP) {
			counts[colour[i]]++;
		}
	}
	ctx.lineWidth = lineWidth(p.view);
	for (let c = 0; c <= FLOW_COLORS; c++) {
		if (!counts[c]) {
			continue;
		}
		ctx.beginPath();
		let pen = -1;
		for (let i = 0; i < n; i++) {
			if (colour[i] !== c) {
				continue;
			}
			if (pen !== i - 1 || s.x0[i] !== s.x1[i - 1] || s.y0[i] !== s.y1[i - 1] || s.z[i] !== s.z[i - 1]) {
				ctx.moveTo(...p.screen(s.x0[i], s.y0[i], s.z[i]));
			}
			ctx.lineTo(...p.screen(s.x1[i], s.y1[i], s.z[i]));
			pen = i;
		}
		if (c === FLOW_COLORS) {
			ctx.strokeStyle = "rgba(128,128,128,0.35)";
			ctx.lineWidth = 0.5;
		} else {
			ctx.strokeStyle = FLOW_RAMP[c];
		}
		ctx.stroke();
	}
}
