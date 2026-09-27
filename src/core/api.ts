import { PLUGIN_ID } from "./backend";
import type { HostAdapter } from "./host";

/**
 * Typed client of the daemon's HTTP API (docs/api.md). Requests go through DWC's REST connector
 * (host.request), which knows the base URL and the session key.
 */

export type QaResult = "running" | "completed" | "cancelled" | "aborted" | "unknown";

export interface JobSummaryExcerpt {
	abortReason: string | null;
	layers: number | null;
	events: Record<string, number>;
	filamentRatio: Record<string, number>;
	avgPercentage: Record<string, number>;
	heaterLoadMean: Record<string, number>;
	heaterLoadEvents: Record<string, number>;
	spoolUsageG: Record<string, number> | null;
}

export interface JobEntry {
	id: string;
	file: string;
	result: "running" | "finished" | "cancelled" | "aborted" | "unknown";
	printTimeS: number | null;
	timestamp: string | null;
	analysable: boolean;
	qaResult: QaResult;
	startedAt: string;
	endedAt: string | null;
	partial: boolean;
	numLayers: number | null;
	material: string | null;
	rawPruned: boolean;
	summary: JobSummaryExcerpt | null;
}

export interface JobDetail extends Omit<JobEntry, "summary"> {
	fileCrc32: string | null;
	startLayer: number | null;
	durationS: number | null;
	warmupS: number | null;
	pauseS: number | null;
	context: Record<string, any> | null;
	summary: Record<string, any> | null;
}

export interface Stats {
	min: number;
	max: number;
	mean: number;
	std: number;
}

export interface LayerRecord {
	layer: number;
	startedAt: string | null;
	endedAt: string | null;
	durationS: number | null;
	height: number | null;
	z: number | null;
	fractionPrinted: number | null;
	filament: Record<string, { commandedMm: number | null; measuredMm: number | null; extruderMm: number | null; ratio?: number }>;
	flow: Record<string, number>;
	temps: {
		heaters: Record<string, Stats & { setpoint: number | null }>;
		sensors: Record<string, Stats & { name: string | null }>;
		chamber: Stats | null;
	};
	fmStats: Record<string, Stats & { avgPercentage: number | null; mmPerRev: number | null }>;
	pwmStats: Record<string, { avgPwmMean: number; avgPwmStd: number; currentStd: number | null; timeS: number }>;
	loadStats: Record<string, {
		mean: number; max: number; p95: number; maxMean60: number | null; atSetpointS: number;
		shareAtSetpoint?: number; shareHigh: number; shareLimit: number; setpoint: number | null;
	} | null>;
}

export interface LayersAnswer {
	jobId: string;
	meta: {
		sensors: Array<{ index: number; name: string | null; type: string | null }>;
		heaters: Array<{ index: number; role: "nozzle" | "bed" | "chamber" | "other"; tool: number | null; sensor: number | null; sensorName: string | null }>;
		chamber: ["sensor" | "heater", number] | null;
		filamentDiameters: Record<string, number | null>;
		heaterLoad: { high: number; limit: number };
	};
	layers: Array<LayerRecord>;
}

export interface QaEvent {
	id: number;
	ts_ms: number;
	ts: string;
	end_ms: number | null;
	type: string;
	subtype: string | null;
	layer: number | null;
	x: number | null;
	y: number | null;
	z: number | null;
	positions: Record<string, number> | null;
	workplace: number | null;
	offsets: Record<string, number> | null;
	tool: number | null;
	object_id: number | null;
	device: number | null;
	payload: Record<string, any> | null;
	block_id: number | null;
}

export type Resolution = "coarse" | "fine" | "auto";

export interface SamplesAnswer {
	jobId: string;
	from: number;
	to: number;
	resolution: Resolution;
	downsampled: boolean;
	channels: Record<string, Array<[number, number]>>;
}

export interface TrendPoint {
	jobId: string;
	ts: string;
	result: QaResult;
	material: string | null;
	value: number;
	[key: string]: any;
}

export type TrendMetric = "heater_load_mean" | "fm_avg_percentage" | "filament_ratio" | "esteps_suggested"
	| "mm_per_rev" | "heat_up_s" | "duration_s" | "events" | "spectrum_peak_hz" | "spectrum_rms";

export type Axis = "X" | "Y" | "Z";

/** One axis of an accelerometer recording (docs/api.md "Spectra") */
export interface Spectrum {
	id: number;
	ts_ms: number;
	layer: number | null;
	/** CAN address of the board carrying the accelerometer */
	board: number | null;
	axis: Axis;
	/** Measured rate of the run (Hz) */
	sampling_rate: number;
	n_samples: number;
	/** Hz, ascending */
	freqs: Array<number>;
	/** g, like DWC's input-shaping plugin (Hann window, 4/N) */
	amplitudes: Array<number>;
	peak_hz: number | null;
	/** g, without the mean */
	rms: number;
	/** Accelerometer port, e.g. 60.i2c.lis */
	source: string | null;
	job_id?: string;
	ts?: string;
}

export interface ReferenceSpectrum {
	axis: Axis;
	mode: "auto" | "manual";
	setAt: string | null;
	spectrumIds: Array<number>;
	jobIds: Array<string>;
	/** auto: false while fewer than ``autoCount`` spectra exist */
	complete: boolean;
	freqs: Array<number>;
	amplitudes: Array<number>;
	peakHz: number | null;
	rms: number | null;
}

export interface AccelerometerStatus {
	enabled: boolean;
	reason?: string | null;
	accelerometer?: { index: number; port: string; board: number; samplingRate: number; resolution: number } | null;
	intervalMin?: number;
	pending?: boolean;
	lastRecording?: number | null;
	lastError?: string | null;
}

export interface StatusAnswer {
	version: string;
	uptimeS: number;
	collector: { state: "idle" | "recording"; currentJobId: string | null; lastJobId: string | null; layer: number | null } | null;
	database: { sizeBytes: number | null; startup: string; queue: number | null; lastError: string | null; lastBackup: string | null };
	settingsErrors: Array<string>;
	liveClients: number;
	timelapse: Record<string, any>;
	accelerometer: AccelerometerStatus;
}

export interface ToolpathAnswer {
	layer: number;
	segments: {
		x0: Array<number>; y0: Array<number>; x1: Array<number>; y1: Array<number>; z: Array<number>;
		e: Array<number>; flow: Array<number>; object: Array<number | null>; type: Array<number>; travel: Array<0 | 1>;
	};
	types: Array<string | null>;
	objects: Record<string, string>;
	meta: { numLayers: number; source: "comments" | "z"; objects: Record<string, string>; filamentDiameter: number };
}

export type TimelapseStatus = "none" | "capturing" | "queued" | "encoding" | "done" | "failed" | "pruned";

/** One snapshot: the frame that shows ``layer`` finished; ``frame`` null when it was skipped or failed */
export interface TimelapseEntry {
	layer: number;
	frame: number | null;
	ts: number;
	reason?: string;
}

export interface TimelapseMeta {
	jobId: string;
	status: TimelapseStatus;
	/** Why nothing was recorded (status none) */
	reason?: string | null;
	codec?: string | null;
	fps?: number | null;
	frames?: number | null;
	sizeBytes?: number | null;
	error?: string | null;
	/** ``job/timelapse`` has the video */
	video: boolean;
	/** In capture order, so frame numbers ascend */
	layers: Array<TimelapseEntry>;
}

const TIMEOUT_MS = 15000;
/** A video of a long job is tens of MB */
const VIDEO_TIMEOUT_MS = 180000;

/**
 * HTTP status of a failed request. DWC's REST connector (@duet3d/connectors RestConnector.request)
 * turns 404 into FileNotFoundError, 401/403 into InvalidPasswordError, other 4xx into
 * OperationFailedError("bad status code <n>") and 5xx into OperationFailedError(<body>)
 */
export function statusOf(error: unknown): number | null {
	const name = (error as { name?: string })?.name ?? "";
	if (name === "FileNotFoundError") {
		return 404;
	}
	if (name === "InvalidPasswordError") {
		return 401;
	}
	const match = /bad status code (\d+)/.exec(error instanceof Error ? error.message : String(error));
	return match ? Number(match[1]) : null;
}

export class QaApi {
	constructor(private readonly host: HostAdapter) {}

	private get<T>(path: string, params: Record<string, string | number | boolean> | null = null, responseType: XMLHttpRequestResponseType = "json",
		timeout = TIMEOUT_MS): Promise<T> {
		return this.host.request("GET", `machine/${PLUGIN_ID}/${path}`, params, responseType, null, timeout) as Promise<T>;
	}

	private post<T>(path: string, body: unknown): Promise<T> {
		return this.host.request("POST", `machine/${PLUGIN_ID}/${path}`, null, "json", JSON.stringify(body), TIMEOUT_MS) as Promise<T>;
	}

	status() {
		return this.get<StatusAnswer>("status");
	}

	settings() {
		return this.get<{ settings: Record<string, any>; errors: Array<string> }>("settings");
	}

	/** Always resolves with ``saved``; ``errors`` names every invalid value */
	saveSettings(settings: Record<string, any>) {
		return this.post<{ saved: boolean; settings: Record<string, any>; errors: Array<string> }>("settings", settings);
	}

	jobs(params: { limit?: number; offset?: number; result?: string; material?: string } = {}) {
		const query: Record<string, string | number> = {};
		for (const [key, value] of Object.entries(params)) {
			if (value !== undefined && value !== null && value !== "") {
				query[key] = value;
			}
		}
		return this.get<{ total: number; offset: number; jobs: Array<JobEntry> }>("jobs", query);
	}

	job(id: string) {
		return this.get<JobDetail>("job", { id });
	}

	layers(id: string) {
		return this.get<LayersAnswer>("job/layers", { id });
	}

	events(id: string, type?: string) {
		return this.get<{ jobId: string; events: Array<QaEvent> }>("job/events", type ? { id, type } : { id });
	}

	samples(id: string, channels: Array<string>, options: { from?: number; to?: number; resolution?: Resolution } = {}) {
		const params: Record<string, string | number> = { id, channels: channels.join(","), resolution: options.resolution ?? "auto" };
		if (options.from !== undefined) {
			params.from = options.from;
		}
		if (options.to !== undefined) {
			params.to = options.to;
		}
		return this.get<SamplesAnswer>("job/samples", params);
	}

	blocks(id: string) {
		return this.get<{ jobId: string; blocks: Array<{ id: number; start_ms: number; end_ms: number | null; triggers: Array<{ ts: number; reason: string }> }> }>("job/blocks", { id });
	}

	channels() {
		return this.get<{ channels: Array<string>; derived: Array<string> }>("channels");
	}

	trends(metric: TrendMetric, params: { limit?: number; material?: string } = {}) {
		const query: Record<string, string | number> = { metric };
		if (params.limit) {
			query.limit = params.limit;
		}
		if (params.material) {
			query.material = params.material;
		}
		return this.get<{ metric: TrendMetric; points: Array<TrendPoint> }>("trends", query);
	}

	toolpath(id: string, layer: number) {
		return this.get<ToolpathAnswer | { state: "building" }>("job/toolpath", { id, layer });
	}

	/** The export as a Blob (DSF sends it as a file) */
	exportBlob(id: string) {
		return this.get<Blob>("job/export", { id }, "blob");
	}

	spectra(id: string) {
		return this.get<{ jobId: string; spectra: Array<Spectrum> }>("job/spectra", { id });
	}

	references() {
		return this.get<{ references: Array<ReferenceSpectrum>; autoCount: number }>("spectra/reference");
	}

	/** A spectrum as the axis' reference, or back to the automatic one with ``null`` */
	setReference(axis: Axis, spectrumId: number | null) {
		return this.post<{ references: Array<ReferenceSpectrum>; autoCount: number }>("spectra/reference",
			spectrumId === null ? { axis, mode: "auto" } : { axis, spectrumId });
	}

	/** The newest spectrum of each of the last ``limit`` jobs, newest first */
	latestSpectra(axis: Axis, limit = 5) {
		return this.get<{ axis: Axis; spectra: Array<Spectrum> }>("spectra/latest", { axis, limit });
	}

	timelapseMeta(id: string) {
		return this.get<TimelapseMeta>("job/timelapse/meta", { id });
	}

	/**
	 * The AV1/MP4 video as a Blob. DSF sends files as application/octet-stream without Range
	 * support, so a `<video>` seeks reliably only in an object URL; a bare URL would also lack
	 * the session key (docs/api.md "Authentication")
	 */
	timelapseVideo(id: string) {
		return this.get<Blob>("job/timelapse", { id }, "blob", VIDEO_TIMEOUT_MS);
	}

	/** JPEG of a layer's frame as a Blob */
	timelapseFrame(id: string, layer: number) {
		return this.get<Blob>("job/timelapse/frame", { id, layer }, "blob");
	}
}
