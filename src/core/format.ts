/** Formatting helpers (no DWC dependency). */

export function formatDuration(seconds: number | null | undefined): string {
	if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
		return "—";
	}
	const s = Math.max(0, Math.round(seconds));
	const h = Math.floor(s / 3600);
	const m = Math.floor((s % 3600) / 60);
	const rest = s % 60;
	if (h > 0) {
		return `${h}h ${String(m).padStart(2, "0")}m`;
	}
	if (m > 0) {
		return `${m}m ${String(rest).padStart(2, "0")}s`;
	}
	return `${rest}s`;
}

/** Elapsed time on a chart axis (seconds since the job started) */
export function formatClock(seconds: number): string {
	const s = Math.max(0, Math.round(seconds));
	const h = Math.floor(s / 3600);
	const m = Math.floor((s % 3600) / 60);
	const rest = s % 60;
	return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(rest).padStart(2, "0")}` : `${m}:${String(rest).padStart(2, "0")}`;
}

export function formatDateTime(iso: string | null | undefined, locale?: string): string {
	if (!iso) {
		return "—";
	}
	const date = new Date(iso);
	return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString(locale);
}

export function formatNumber(value: number | null | undefined, digits = 1, unit = ""): string {
	if (value === null || value === undefined || !Number.isFinite(value)) {
		return "—";
	}
	return `${value.toFixed(digits)}${unit ? ` ${unit}` : ""}`;
}

export function formatPercent(fraction: number | null | undefined, digits = 0): string {
	if (fraction === null || fraction === undefined || !Number.isFinite(fraction)) {
		return "—";
	}
	return `${(fraction * 100).toFixed(digits)} %`;
}

export function formatBytes(bytes: number | null | undefined): string {
	if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) {
		return "—";
	}
	const units = ["B", "KiB", "MiB", "GiB", "TiB"];
	let value = bytes;
	let unit = 0;
	while (value >= 1024 && unit < units.length - 1) {
		value /= 1024;
		unit++;
	}
	return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function fileName(path: string | null | undefined): string {
	if (!path) {
		return "—";
	}
	const index = path.lastIndexOf("/");
	return index >= 0 ? path.substring(index + 1) : path;
}

/** Colour of a job result / event severity (Vuetify colour names) */
export function resultColor(result: string): string {
	switch (result) {
		case "finished":
		case "completed":
			return "success";
		case "running":
			return "info";
		case "cancelled":
			return "warning";
		case "aborted":
			return "error";
		default:
			return "grey";
	}
}

const EVENT_COLORS: Record<string, string> = {
	heater_fault: "error",
	driver_error: "error",
	heater_monitor: "error",
	voltage_dip: "error",
	phantom_reading: "warning",
	filament_status: "warning",
	filament_percent_window: "warning",
	filament_percent_level: "warning",
	filament_percent_drift: "warning",
	heater_load: "warning",
	mfm_error_tolerated: "warning",
	mfm_recovery: "warning",
	mfm_flow_bias: "warning",
	gear_passes: "warning",
	gear_passes_forecast: "warning",
	pause: "info",
	resume: "info",
	setpoint_change: "primary",
	babystep: "primary",
	calibration: "primary",
	job_start: "success",
	job_end: "success",
	daemon_started_mid_job: "grey",
	timelapse_failed: "warning",
	accelerometer_failed: "warning"
};

export function eventColor(type: string): string {
	return EVENT_COLORS[type] ?? "grey";
}

/** Short, language-neutral detail of an event payload for a table cell */
function plain(value: unknown): string {
	if (value === null || value === undefined) {
		return "—";
	}
	return typeof value === "object" ? JSON.stringify(value) : String(value);
}

/** A setpoint's change; of an object (fm.configured, tool.retraction) only the fields that changed */
function setpointChange(from: unknown, to: unknown): string {
	const isObject = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
	if (!isObject(from) && !isObject(to)) {
		return `${plain(from)} → ${plain(to)}`;
	}
	const a = isObject(from) ? from : {};
	const b = isObject(to) ? to : {};
	const changed = [...new Set([...Object.keys(a), ...Object.keys(b)])].filter((k) => JSON.stringify(a[k]) !== JSON.stringify(b[k]));
	return changed.map((k) => `${k}: ${plain(a[k])} → ${plain(b[k])}`).join(", ") || "—";
}

/** A calibration in the terms RRF reports it with (qa_calibration.py): μ mean, σ deviation */
function calibrationDetail(kind: string | null, p: Record<string, any>): string {
	const mm = (value: unknown) => formatNumber(value as number, 3);
	switch (kind) {
		case "mesh":   // G29: probed points, min … max error
			return `${p.points !== undefined ? `${p.points}: ` : ""}${p.minError !== undefined ? `${mm(p.minError)} … ${mm(p.maxError)} mm, ` : ""}μ ${mm(p.mean)}, σ ${mm(p.deviation)}`;
		case "levelling":   // G32: the leadscrew corrections, σ before → after
			return `${Array.isArray(p.corrections) ? `${p.corrections.map(mm).join(" ")} mm, ` : ""}σ ${mm(p.before?.deviation)} → ${mm(p.after?.deviation)}`;
		case "probe":   // M558.1: offset at the trigger reading and the fit's rms error; else G31's trigger height
			return Array.isArray(p.scanCoefficients)
				? `${mm(p.scanCoefficients[0])} mm @ ${p.threshold ?? "—"}${p.rmsError !== undefined ? `, rms ${mm(p.rmsError)} mm` : ""}`
				: `Z ${mm(p.triggerHeight)} mm`;
		case "probeDrive":   // M558.2
			return `I ${p.current ?? "—"}, offset ${p.offset ?? "—"}`;
		default:
			return "";
	}
}

export function eventDetail(event: { type: string; subtype: string | null; payload: Record<string, any> | null }): string {
	const p = event.payload ?? {};
	switch (event.type) {
		case "setpoint_change":
			// an axis (M350) is only in the payload; a numeric index is the event's device (#n)
			return `${typeof p.index === "string" ? `${p.index}: ` : ""}${setpointChange(p.from, p.to)}${p.cause ? ` (${p.cause})` : ""}`;
		case "heater_load":
			return `${formatPercent(p.peakMean)} @ ${p.setpoint ?? "—"} °C${p.durationS !== undefined ? `, ${formatDuration(p.durationS)}` : ""}`;
		case "filament_status":
		case "filament_percent_window":
			return `${p.lastPercentage ?? "—"} %${p.durationS !== undefined ? `, ${formatDuration(p.durationS)}` : ""}`;
		case "filament_percent_drift":   // the level, the layer mean farthest from it, the run of layers
			return `${p.level ?? "—"} % → ${formatNumber(p.extreme, 0)} % (L${p.firstLayer ?? "?"}–${p.lastLayer ?? "?"})`;
		case "filament_percent_level":   // from the monitor's level in the job to its farthest reading
			return `${p.level ?? "—"} % → ${p.extreme ?? p.lastPercentage ?? "—"} %${p.durationS !== undefined ? `, ${formatDuration(p.durationS)}` : ""}`;
		case "voltage_dip":
			return `${formatNumber(p.vIn, 1, "V")} (${formatNumber(p.median90s, 1, "V")})`;
		case "phantom_reading":
			return `${formatNumber(p.before, 1)} → ${formatNumber(p.peak, 1)} °C`;
		case "driver_error": {
			if (p.text) {
				return p.text;  // RRF's own message names the driver already
			}
			const driver = p.driver !== undefined && p.driver !== null ? `${p.canAddress ?? p.board ?? "?"}.${p.driver}: ` : "";
			const duration = typeof p.durationS === "number" ? `, ${p.durationS} s` : "";  // an open-load episode
			return driver + (Array.isArray(p.bits) ? p.bits.join(", ") : "") + duration;
		}
		case "resume":
			return formatDuration(p.pausedS);
		case "mfm_error_tolerated":
			return `#${p.count ?? "?"}`;
		case "gear_passes_forecast":   // the runs of layers expected at the threshold or more
			return (p.runs ?? []).map((r: any) => `L${r.firstLayer}–${r.lastLayer} (${formatNumber(r.max, 1)}×)`).join(", ");
		case "gear_passes":
			return `${formatNumber(p.max, 1)}× @ L${p.maxLayer ?? "?"} (L${p.firstLayer ?? "?"}–${p.lastLayer ?? "?"})`;
		case "babystep":
			return `${p.from ?? "—"} → ${p.to ?? "—"} mm`;
		case "calibration":
			return calibrationDetail(event.subtype, p);
		case "timelapse_failed":
		case "accelerometer_failed":
			return p.error ?? "";
		default:
			return "";
	}
}
