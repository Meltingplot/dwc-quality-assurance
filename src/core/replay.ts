/**
 * Replay helpers (PLAN.md §5.8): geometry and colours for drawing one layer's toolpath with the
 * measured samples and events on top. Pure functions, no DOM.
 */
import type { QaEvent, ToolpathAnswer } from "./api";

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
 * Measured points: every extrusion-rate sample from ``from`` on, placed at the machine X/Y valid at
 * its time. A value holds until the next sample of its channel (fine blocks store only changed
 * values, so the samples should start a coarse interval before ``from``). ``machinePosition`` is
 * the position of the move being performed or of the last one (DuetAPI ``Axis.MachinePosition``,
 * DuetSoftwareFramework v3.7-dev @ cd3ae65f, 2026-09-27), so a point sits where its move ends.
 * ``extrusionRate`` is filament mm/s (``CurrentMove.ExtrusionRate``, same source); times
 * ``area`` it is mm³/s like the commanded flow.
 */
export function measuredPoints(channels: Record<string, Array<[number, number]>>, area: number, from = -Infinity) {
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
