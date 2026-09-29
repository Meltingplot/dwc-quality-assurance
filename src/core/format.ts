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
	heater_load: "warning",
	mfm_error_tolerated: "warning",
	mfm_recovery: "warning",
	mfm_flow_bias: "warning",
	gear_passes: "warning",
	pause: "info",
	resume: "info",
	setpoint_change: "primary",
	babystep: "primary",
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
export function eventDetail(event: { type: string; subtype: string | null; payload: Record<string, any> | null }): string {
	const p = event.payload ?? {};
	switch (event.type) {
		case "setpoint_change":
			return `${p.from ?? "—"} → ${p.to ?? "—"}${p.cause ? ` (${p.cause})` : ""}`;
		case "heater_load":
			return `${formatPercent(p.peakMean)} @ ${p.setpoint ?? "—"} °C${p.durationS !== undefined ? `, ${formatDuration(p.durationS)}` : ""}`;
		case "filament_status":
		case "filament_percent_window":
			return `${p.lastPercentage ?? "—"} %${p.durationS !== undefined ? `, ${formatDuration(p.durationS)}` : ""}`;
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
		case "gear_passes":
			return `${formatNumber(p.max, 1)}× @ L${p.maxLayer ?? "?"} (L${p.firstLayer ?? "?"}–${p.lastLayer ?? "?"})`;
		case "babystep":
			return `${p.from ?? "—"} → ${p.to ?? "—"} mm`;
		case "timelapse_failed":
		case "accelerometer_failed":
			return p.error ?? "";
		default:
			return "";
	}
}
