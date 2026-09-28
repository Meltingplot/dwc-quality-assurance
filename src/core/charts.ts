/**
 * Chart.js 4 configurations. The builders return plain objects, so charting is unit-tested
 * without a canvas and the components only create, apply and destroy (as in dwc-vigil).
 *
 * The only module that imports Chart.js: DWC 3.7 does not expose it to plugins, so the plugin
 * bundles its own copy (`chart.js` in package.json; the DWC builder externalises only DWC's
 * own modules). Time runs on a linear axis in seconds since the job started, so no date
 * adapter is needed.
 */
import Chart from "chart.js/auto";

import { formatClock } from "./format";

export { Chart };

export const PALETTE = ["#1976D2", "#E53935", "#43A047", "#FB8C00", "#8E24AA", "#00ACC1", "#6D4C41", "#546E7A", "#D81B60", "#7CB342"];

export function color(index: number): string {
	return PALETTE[index % PALETTE.length];
}

/** Redraw an existing chart from a freshly built config */
export function applyConfig(chart: { data: any; options: any; update: (mode?: any) => void }, config: { data: any; options: any }) {
	chart.data = config.data;
	chart.options = config.options;
	chart.update("none");
}

const STATIC = {
	responsive: true,
	maintainAspectRatio: false,
	animation: { duration: 0 },
	parsing: false as const,
	normalized: true
};

const SUBTLE_GRID = { grid: { color: "rgba(128,128,128,0.15)" }, border: { display: false } };

export interface TimeSeries {
	label: string;
	unit?: string;
	/** [epoch ms, value] */
	points: Array<[number, number]>;
	/** Right-hand axis (e.g. load 0..1 next to temperatures) */
	secondary?: boolean;
	stepped?: boolean;
}

export interface Marker {
	/** epoch ms */
	ts: number;
	label: string;
	color?: string;
}

/**
 * Line chart over job time. ``startMs`` is the zero of the x axis; ``markers`` are drawn as
 * vertical lines (events, layer changes)
 */
export function timeSeriesConfig(series: Array<TimeSeries>, startMs: number, options: { markers?: Array<Marker>; yLabel?: string; y2Label?: string } = {}) {
	const hasSecondary = series.some((s) => s.secondary);
	return {
		type: "line" as const,
		data: {
			datasets: series.map((s, i) => ({
				label: s.unit ? `${s.label} (${s.unit})` : s.label,
				data: s.points.map(([ts, value]) => ({ x: (ts - startMs) / 1000, y: value })),
				borderColor: color(i),
				backgroundColor: color(i),
				borderWidth: 1.5,
				pointRadius: 0,
				stepped: s.stepped ? "before" as const : false as const,
				yAxisID: s.secondary ? "y2" : "y"
			}))
		},
		options: {
			...STATIC,
			interaction: { mode: "nearest" as const, axis: "x" as const, intersect: false },
			scales: {
				x: { type: "linear" as const, ...SUBTLE_GRID, ticks: { callback: (value: number | string) => formatClock(Number(value)), maxTicksLimit: 10 } },
				y: { type: "linear" as const, ...SUBTLE_GRID, title: { display: !!options.yLabel, text: options.yLabel ?? "" } },
				...(hasSecondary ? { y2: { type: "linear" as const, position: "right" as const, grid: { display: false }, title: { display: !!options.y2Label, text: options.y2Label ?? "" } } } : {})
			},
			plugins: {
				legend: { position: "bottom" as const, labels: { boxWidth: 12 } },
				tooltip: { callbacks: { title: (items: Array<{ parsed: { x: number } }>) => (items.length ? formatClock(items[0].parsed.x) : "") } },
				qaMarkers: { markers: (options.markers ?? []).map((m) => ({ ...m, x: (m.ts - startMs) / 1000 })) }
			}
		}
	};
}

export interface LayerSeries {
	label: string;
	unit?: string;
	values: Array<number | null>;
	type?: "bar" | "line";
	secondary?: boolean;
}

/** Values per layer (x = layer number) */
export function layerChartConfig(layers: Array<number>, series: Array<LayerSeries>, options: { stacked?: boolean; yMin?: number; yMax?: number } = {}) {
	return {
		type: "bar" as const,
		data: {
			labels: layers.map(String),
			datasets: series.map((s, i) => ({
				type: s.type ?? "bar",
				label: s.unit ? `${s.label} (${s.unit})` : s.label,
				data: s.values,
				borderColor: color(i),
				backgroundColor: s.type === "line" ? color(i) : `${color(i)}99`,
				borderWidth: s.type === "line" ? 1.5 : 0,
				pointRadius: 0,
				yAxisID: s.secondary ? "y2" : "y"
			}))
		},
		options: {
			...STATIC,
			parsing: undefined,
			scales: {
				x: { ...SUBTLE_GRID, stacked: options.stacked ?? false, ticks: { autoSkip: true, maxTicksLimit: 20 } },
				y: { ...SUBTLE_GRID, stacked: options.stacked ?? false, min: options.yMin, max: options.yMax },
				...(series.some((s) => s.secondary) ? { y2: { position: "right" as const, grid: { display: false } } } : {})
			},
			plugins: { legend: { position: "bottom" as const, labels: { boxWidth: 12 } } }
		}
	};
}

/** Histogram from classes ``[{from, to, s}]`` (e.g. the monitor percentage distribution) */
export function histogramConfig(label: string, classes: Array<{ from: number; to: number; s: number }>) {
	return {
		type: "bar" as const,
		data: {
			labels: classes.map((c) => `${c.from}–${c.to}`),
			datasets: [{ label, data: classes.map((c) => c.s), backgroundColor: `${color(0)}99`, borderWidth: 0 }]
		},
		options: {
			...STATIC,
			parsing: undefined,
			scales: { x: { ...SUBTLE_GRID }, y: { ...SUBTLE_GRID } },
			plugins: { legend: { display: false } }
		}
	};
}

export interface TrendSeries {
	label: string;
	/** [epoch ms, value] per job */
	points: Array<{ ts: number; value: number; jobId: string }>;
}

/** Values over jobs (x = job start as days before now, newest right) */
export function trendConfig(series: Array<TrendSeries>, nowMs: number, yLabel = "") {
	return {
		type: "line" as const,
		data: {
			datasets: series.map((s, i) => ({
				label: s.label,
				data: s.points.map((p) => ({ x: (p.ts - nowMs) / 86_400_000, y: p.value, jobId: p.jobId })),
				borderColor: color(i),
				backgroundColor: color(i),
				borderWidth: 1,
				pointRadius: 3,
				showLine: true
			}))
		},
		options: {
			...STATIC,
			scales: {
				// days before now: ends at now and shows at least a week, so a single job is not a hairline scale
				x: { type: "linear" as const, max: 0, suggestedMin: -7, ...SUBTLE_GRID, title: { display: true, text: "d" } },
				y: { type: "linear" as const, ...SUBTLE_GRID, title: { display: !!yLabel, text: yLabel } }
			},
			plugins: { legend: { position: "bottom" as const, labels: { boxWidth: 12 } } }
		}
	};
}

export interface SpectrumSeries {
	label: string;
	freqs: Array<number>;
	amplitudes: Array<number>;
	/** the reference: dashed */
	dashed?: boolean;
	/** drawn faint (context, e.g. the other recordings of a job) */
	faint?: boolean;
}

/** Amplitude (g) over frequency (Hz) */
export function spectrumConfig(series: Array<SpectrumSeries>, yLabel = "g") {
	return {
		type: "line" as const,
		data: {
			datasets: series.map((s, i) => ({
				label: s.label,
				data: s.freqs.map((f, k) => ({ x: f, y: s.amplitudes[k] })),
				borderColor: s.dashed ? "#9E9E9E" : color(i),
				backgroundColor: s.dashed ? "#9E9E9E" : color(i),
				borderWidth: s.faint ? 0.75 : 1.5,
				borderDash: s.dashed ? [6, 4] : undefined,
				pointRadius: 0
			}))
		},
		options: {
			...STATIC,
			interaction: { mode: "nearest" as const, axis: "x" as const, intersect: false },
			scales: {
				x: { type: "linear" as const, ...SUBTLE_GRID, min: 0, title: { display: true, text: "Hz" } },
				y: { type: "linear" as const, ...SUBTLE_GRID, min: 0, title: { display: !!yLabel, text: yLabel } }
			},
			plugins: { legend: { position: "bottom" as const, labels: { boxWidth: 12 } } }
		}
	};
}

/** Draws the ``plugins.qaMarkers.markers`` of a chart as vertical lines */
export const markerPlugin = {
	id: "qaMarkers",
	afterDatasetsDraw(chart: any, _args: unknown, options: { markers?: Array<{ x: number; label: string; color?: string }> }) {
		const markers = options?.markers ?? [];
		const x = chart.scales?.x;
		const area = chart.chartArea;
		if (!x || !area || markers.length === 0) {
			return;
		}
		const ctx = chart.ctx;
		ctx.save();
		for (const marker of markers) {
			const px = x.getPixelForValue(marker.x);
			if (px < area.left || px > area.right) {
				continue;
			}
			ctx.strokeStyle = marker.color ?? "rgba(229,57,53,0.7)";
			ctx.lineWidth = 1;
			ctx.setLineDash([4, 3]);
			ctx.beginPath();
			ctx.moveTo(px, area.top);
			ctx.lineTo(px, area.bottom);
			ctx.stroke();
		}
		ctx.restore();
	}
};

let registered = false;

/** Register QA's chart plugins once (idempotent) */
export function registerChartPlugins() {
	if (!registered) {
		Chart.register(markerPlugin);
		registered = true;
	}
}
