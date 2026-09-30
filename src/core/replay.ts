/**
 * Replay helpers (PLAN.md §5.8): geometry and colours for drawing one layer's toolpath with the
 * samples of the executed moves and events on top, shared by the replay tab and the embeddable layer view.
 * Pure functions plus the toolpath request, no DOM.
 */
import { statusOf, type LayersAnswer, type QaApi, type QaEvent, type ToolpathAnswer } from "./api";
import { eventColor } from "./format";

export interface Bounds {
	minX: number;
	minY: number;
	maxX: number;
	maxY: number;
}

/** Bounds of the segments (travel included, so the view does not jump when it is shown) */
export function toolpathBounds(tp: ToolpathAnswer, includeTravel = true): Bounds | null {
	const s = tp.segments;
	let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
	for (let i = 0; i < s.x0.length; i++) {
		if (!includeTravel && s.travel[i]) {
			continue;
		}
		minX = Math.min(minX, s.x0[i], s.x1[i]);
		maxX = Math.max(maxX, s.x0[i], s.x1[i]);
		minY = Math.min(minY, s.y0[i], s.y1[i]);
		maxY = Math.max(maxY, s.y0[i], s.y1[i]);
	}
	return Number.isFinite(minX) ? { minX, minY, maxX, maxY } : null;
}

export interface View {
	scale: number;
	/** screen = (world - origin) * scale, y flipped */
	originX: number;
	originY: number;
	height: number;
}

/** A view that fits ``bounds`` into a canvas of ``width`` × ``height`` with a margin */
export function fitView(bounds: Bounds, width: number, height: number, margin = 16): View {
	const w = Math.max(bounds.maxX - bounds.minX, 1e-3);
	const h = Math.max(bounds.maxY - bounds.minY, 1e-3);
	const scale = Math.min((width - 2 * margin) / w, (height - 2 * margin) / h);
	const originX = bounds.minX - (width / scale - w) / 2;
	const originY = bounds.minY - (height / scale - h) / 2;
	return { scale, originX, originY, height };
}

export function toScreen(view: View, x: number, y: number): [number, number] {
	return [(x - view.originX) * view.scale, view.height - (y - view.originY) * view.scale];
}

export function toWorld(view: View, px: number, py: number): [number, number] {
	return [px / view.scale + view.originX, (view.height - py) / view.scale + view.originY];
}

/** Zoom by ``factor`` around the screen point (px, py) */
export function zoomAt(view: View, factor: number, px: number, py: number): View {
	const [wx, wy] = toWorld(view, px, py);
	const scale = Math.min(Math.max(view.scale * factor, 0.05), 5000);
	return { scale, originX: wx - px / scale, originY: wy - (view.height - py) / scale, height: view.height };
}

export function pan(view: View, dx: number, dy: number): View {
	return { ...view, originX: view.originX - dx / view.scale, originY: view.originY + dy / view.scale };
}

/** Viridis-like ramp for flow values, u in 0..1 */
const RAMP: Array<[number, number, number]> = [
	[68, 1, 84], [59, 82, 139], [33, 145, 140], [94, 201, 98], [253, 231, 37]
];

export function rampColor(u: number): string {
	const t = Math.max(0, Math.min(1, Number.isFinite(u) ? u : 0)) * (RAMP.length - 1);
	const i = Math.min(RAMP.length - 2, Math.floor(t));
	const f = t - i;
	const a = RAMP[i], b = RAMP[i + 1];
	return `rgb(${Math.round(a[0] + (b[0] - a[0]) * f)},${Math.round(a[1] + (b[1] - a[1]) * f)},${Math.round(a[2] + (b[2] - a[2]) * f)})`;
}

/** Flow range of the extruding segments: 5th and 95th percentile, so a few outliers do not flatten the scale */
export function flowRange(tp: ToolpathAnswer): [number, number] {
	const values = tp.segments.flow.filter((f, i) => !tp.segments.travel[i] && f > 0).sort((a, b) => a - b);
	if (values.length === 0) {
		return [0, 1];
	}
	const lo = values[Math.floor(values.length * 0.05)];
	const hi = values[Math.min(values.length - 1, Math.floor(values.length * 0.95))];
	return hi - lo < 1e-6 ? [lo * 0.9, lo * 1.1 + 1e-6] : [lo, hi];
}

/**
 * Machine position to G-code (user) coordinates: workplace offsets are subtracted from machine
 * coordinates and tool offsets from the printing locations (wiki Gcodes.md G10; worked example with
 * signs in Tuning/Defining_tool_and_Z_probe_offsets.md; 2026-09-27), so
 * user = machine − workplace offset + tool offset. Bed compensation, skew and rotation are not
 * undone; for markers on the replay that is close enough.
 */
export function machineToUser(machine: { x: number; y: number }, workplace: { X?: number; Y?: number } | null,
	toolOffsets: { X?: number; Y?: number } | null): { x: number; y: number } {
	return {
		x: machine.x - (workplace?.X ?? 0) + (toolOffsets?.X ?? 0),
		y: machine.y - (workplace?.Y ?? 0) + (toolOffsets?.Y ?? 0)
	};
}

/** Tool offsets per axis letter from the job context (``tools[].offsets`` follows the axis order) */
export function toolOffsets(context: Record<string, any> | null | undefined, tool: number | null): { X?: number; Y?: number } {
	if (!context || tool === null || tool === undefined) {
		return {};
	}
	const entry = (context.tools ?? []).find((t: { number: number }) => t.number === tool);
	const letters: Array<string> = (context.axes ?? []).map((a: { letter: string }) => a.letter);
	const result: Record<string, number> = {};
	(entry?.offsets ?? []).forEach((value: number | null, i: number) => {
		if (letters[i] && typeof value === "number") {
			result[letters[i]] = value;
		}
	});
	return result;
}

/**
 * Workplace offsets and tool in force at ``ts``: events carry both (the collector stores them with
 * every event, tool changes are no events of their own), so the latest event at or before ``ts``
 * decides, the earliest one after it when there is none. The tool comes from the nearest such event
 * that has one selected (``state.currentTool`` is −1 before the start G-code picks a tool).
 * ``events`` sorted by ``ts_ms`` as ``job/events`` answers them.
 */
export function frameAt(events: Array<QaEvent>, ts: number): { offsets: Record<string, number> | null; tool: number | null } {
	let lo = 0, hi = events.length;
	while (lo < hi) {
		const mid = (lo + hi) >> 1;
		if (events[mid].ts_ms <= ts) {
			lo = mid + 1;
		} else {
			hi = mid;
		}
	}
	// events[0..lo) are at or before ts: search backwards from there, then forwards
	const nearest = (ok: (e: QaEvent) => boolean): QaEvent | null => {
		for (let i = lo - 1; i >= 0; i--) {
			if (ok(events[i])) {
				return events[i];
			}
		}
		return events.slice(lo).find(ok) ?? null;
	};
	return {
		offsets: nearest((e) => !!e.offsets)?.offsets ?? null,
		tool: nearest((e) => typeof e.tool === "number" && e.tool >= 0)?.tool ?? null
	};
}

/**
 * Executed points: every extrusion-rate sample from ``from`` on, placed at the machine X/Y valid at
 * its time. A value holds until the next sample of its channel (fine blocks store only changed
 * values, so the samples should start a coarse interval before ``from``). ``machinePosition`` is
 * the position of the move being performed or of the last one (DuetAPI ``Axis.MachinePosition``,
 * DuetSoftwareFramework v3.7-dev @ cd3ae65f, 2026-09-27), so a point sits where its move ends.
 * ``extrusionRate`` is filament mm/s (``CurrentMove.ExtrusionRate``, same source): the rate RRF planned for
 * the move it is executing, at that move's top speed, not a measurement (RRF 3.7-dev @ 32a84d2 Move.cpp:213, DDA::GetTotalExtrusionRate DDA.cpp:1686-1694, read 2026-09-29); times
 * ``area`` it is mm³/s like the commanded flow.
 */
export function executedPoints(channels: Record<string, Array<[number, number]>>, area: number, from = -Infinity) {
	const xs = channels["axis.X.machinePosition"] ?? [];
	const ys = channels["axis.Y.machinePosition"] ?? [];
	const points: Array<{ ts: number; x: number; y: number; flow: number }> = [];
	let ix = -1, iy = -1;
	for (const [ts, rate] of channels["move.currentMove.extrusionRate"] ?? []) {
		while (ix + 1 < xs.length && xs[ix + 1][0] <= ts) {
			ix++;
		}
		while (iy + 1 < ys.length && ys[iy + 1][0] <= ts) {
			iy++;
		}
		if (ts >= from && ix >= 0 && iy >= 0 && rate > 0) {
			points.push({ ts, x: xs[ix][1], y: ys[iy][1], flow: rate * area });
		}
	}
	return points;
}

/** A layer's toolpath as the replay shows it: the segments, or why there are none */
export type ToolpathResult =
	| { state: "ready"; toolpath: ToolpathAnswer }
	/** 202: the daemon still indexes the G-code file; ask again */
	| { state: "building" }
	/** 409: the file is gone or was changed after the job */
	| { state: "fileGone" }
	/** 404: the file has no such layer */
	| { state: "noLayer" }
	| { state: "error"; message: string };

export async function fetchToolpath(api: QaApi, jobId: string, layer: number): Promise<ToolpathResult> {
	try {
		const answer = await api.toolpath(jobId, layer);
		return "state" in answer ? { state: "building" } : { state: "ready", toolpath: answer };
	} catch (e) {
		const status = statusOf(e);
		if (status === 409) {
			return { state: "fileGone" };
		}
		if (status === 404) {
			return { state: "noLayer" };
		}
		return { state: "error", message: e instanceof Error ? e.message : String(e) };
	}
}

/** Ask again this long after a 202 while the daemon indexes the file */
export const TOOLPATH_RETRY_MS = 2000;

/**
 * A slider drag or a held arrow on an embedding page passes many layers: the embeddable views
 * ask for the layer it stops at. The HMI's haproxy queues requests beyond four (PLAN.md §5.6)
 */
export const LAYER_SETTLE_MS = 150;

/**
 * Samples start this much before the layer: fine rows hold only changed values, the coarse row
 * before the layer has the positions (two intervals at the default sampleIntervalS of 5 s)
 */
export const SAMPLE_LEAD_MS = 10000;

/** Time span of a layer from ``job/layers``; one still printing ends now */
export function layerSpan(layers: LayersAnswer | null | undefined, layer: number): { from: number; to: number } | null {
	const record = layers?.layers.find((l) => l.layer === layer);
	if (!record?.startedAt) {
		return null;
	}
	return { from: new Date(record.startedAt).getTime(), to: record.endedAt ? new Date(record.endedAt).getTime() : Date.now() };
}

/** Nozzle heaters of a job (``job/layers`` meta) */
export function nozzleHeaters(layers: LayersAnswer | null | undefined): Array<{ index: number; tool: number | null }> {
	return (layers?.meta.heaters ?? []).filter((h) => h.role === "nozzle");
}

/** Sample channels of a layer's replay: position and extrusion rate for the executed points, temperature and load per nozzle */
export function replayChannels(nozzles: Array<{ index: number }>): Array<string> {
	return ["axis.X.machinePosition", "axis.Y.machinePosition", "move.currentMove.extrusionRate",
		...nozzles.flatMap((h) => [`heater.${h.index}.current`, `heater.${h.index}.load`])];
}

const MARKER_COLORS: Record<string, string> = { error: "#E53935", warning: "#FB8C00", info: "#1E88E5", primary: "#1976D2", success: "#43A047", grey: "#9E9E9E" };

/** Ring colour of an event on the canvas (the event's chip colour as RGB) */
export function markerColor(type: string): string {
	return MARKER_COLORS[eventColor(type)] ?? MARKER_COLORS.grey;
}

/** Events a layer's replay marks: the ones of that layer with a position, without job start and end */
export function layerEvents(events: Array<QaEvent>, layer: number): Array<QaEvent> {
	return events.filter((e) => e.layer === layer && !["job_start", "job_end"].includes(e.type));
}

/** Events the 3D stack marks: the ones of every layer up to ``layer`` */
export function stackEvents(events: Array<QaEvent>, layer: number): Array<QaEvent> {
	return events.filter((e) => e.layer !== null && e.layer <= layer && !["job_start", "job_end"].includes(e.type));
}
