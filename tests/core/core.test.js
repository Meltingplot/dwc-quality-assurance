import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QaApi, statusOf } from "../../src/core/api";
import { histogramConfig, layerChartConfig, markerPlugin, timeSeriesConfig, trendConfig } from "../../src/core/charts";
import { eventDetail, fileName, formatBytes, formatClock, formatDuration, formatPercent, resultColor } from "../../src/core/format";
import { LiveClient } from "../../src/core/ws";

describe("QaApi", () => {
	function host() {
		const calls = [];
		return {
			calls,
			request: (method, path, params, responseType, body) => {
				calls.push({ method, path, params, responseType, body });
				return Promise.resolve({ ok: true });
			}
		};
	}

	it("builds the endpoint paths and parameters", async () => {
		const h = host();
		const api = new QaApi(h);
		await api.jobs({ limit: 25, offset: 0, result: "finished", material: "" });
		await api.samples("j1", ["a", "b"], { resolution: "fine", from: 5 });
		await api.events("j1", "pause,resume");
		await api.trends("heater_load_mean", { limit: 10 });
		await api.saveSettings({ sampleIntervalS: 5 });
		await api.exportBlob("j1");
		expect(h.calls[0]).toMatchObject({ method: "GET", path: "machine/QualityAssurance/jobs", params: { limit: 25, offset: 0, result: "finished" } });
		expect(h.calls[1].params).toEqual({ id: "j1", channels: "a,b", resolution: "fine", from: 5 });
		expect(h.calls[2].params).toEqual({ id: "j1", type: "pause,resume" });
		expect(h.calls[3].params).toEqual({ metric: "heater_load_mean", limit: 10 });
		expect(h.calls[4]).toMatchObject({ method: "POST", path: "machine/QualityAssurance/settings", body: "{\"sampleIntervalS\":5}" });
		expect(h.calls[5].responseType).toBe("blob");
	});

	it("reads the status of a failed request", () => {
		expect(statusOf(new Error("bad status code 409"))).toBe(409);
		expect(statusOf(Object.assign(new Error("x"), { name: "FileNotFoundError" }))).toBe(404);
		expect(statusOf(new Error("boom"))).toBeNull();
	});
});

describe("format", () => {
	it("formats durations and clocks", () => {
		expect(formatDuration(3725)).toBe("1h 02m");
		expect(formatDuration(65)).toBe("1m 05s");
		expect(formatDuration(null)).toBe("—");
		expect(formatClock(3725)).toBe("1:02:05");
		expect(formatClock(65)).toBe("1:05");
	});

	it("formats values", () => {
		expect(formatPercent(0.456)).toBe("46 %");
		expect(formatBytes(1536)).toBe("1.5 KiB");
		expect(fileName("0:/gcodes/sub/a.gcode")).toBe("a.gcode");
		expect(resultColor("finished")).toBe("success");
		expect(resultColor("aborted")).toBe("error");
	});

	it("summarises event payloads", () => {
		expect(eventDetail({ type: "setpoint_change", subtype: "stepsPerMm", payload: { from: 790, to: 812.5, cause: "mfm_flow_bias" } }))
			.toBe("790 → 812.5 (mfm_flow_bias)");
		// objects (job 20260928-155257-118609a9, L166): only what changed, not "[object Object]"
		const configured = { allMoves: false, mmPerRev: 25.3, percentMin: 60, percentMax: 180, sampleDistance: 5 };
		expect(eventDetail({ type: "setpoint_change", subtype: "fm.configured", payload: { from: configured, to: { ...configured, allMoves: true } } }))
			.toBe("allMoves: false → true");
		expect(eventDetail({ type: "setpoint_change", subtype: "tool.retraction", payload: { from: null, to: { length: 0.4, zHop: 0 } } }))
			.toBe("length: — → 0.4, zHop: — → 0");
		expect(eventDetail({ type: "setpoint_change", subtype: "fm.calibrated", payload: { from: true, to: false } })).toBe("true → false");
		expect(eventDetail({ type: "setpoint_change", subtype: "heater.active", payload: { from: null, to: 220 } })).toBe("— → 220");
		expect(eventDetail({ type: "setpoint_change", subtype: "axis.microstepping", payload: { index: "Y", from: { value: 16, interpolated: false },
			to: { value: 64, interpolated: false } } })).toBe("Y: value: 16 → 64");
		expect(eventDetail({ type: "setpoint_change", subtype: "shaping", payload: { index: null, from: { type: "none", frequency: 32.8 },
			to: { type: "mzv", frequency: 32.8 } } })).toBe("type: none → mzv");
		expect(eventDetail({ type: "heater_load", subtype: "high", payload: { peakMean: 0.86, setpoint: 220, durationS: 125 } }))
			.toBe("86 % @ 220 °C, 2m 05s");
		expect(eventDetail({ type: "driver_error", subtype: "warning", payload: { source: "status", board: 1, canAddress: 20, driver: 0,
			bits: ["phase B may be disconnected"] } })).toBe("20.0: phase B may be disconnected");
		expect(eventDetail({ type: "driver_error", subtype: "warning", payload: { canAddress: 20, driver: 0, bits: ["phase A may be disconnected"],
			confirmed: false, durationS: 0.3 } })).toBe("20.0: phase A may be disconnected, 0.3 s");
		expect(eventDetail({ type: "driver_error", subtype: "warning", payload: { source: "message", canAddress: 20, driver: 0,
			text: "Driver 20.0 warning: phase A may be disconnected" } })).toBe("Driver 20.0 warning: phase A may be disconnected");
		expect(eventDetail({ type: "gear_passes", subtype: "high", payload: { threshold: 5, firstLayer: 137, lastLayer: 176,
			max: 7.62, maxLayer: 150 } })).toBe("7.6× @ L150 (L137–176)");
		expect(eventDetail({ type: "filament_percent_level", subtype: "low", payload: { level: 95, lastPercentage: 64, extreme: 35,
			durationS: 365 } })).toBe("95 % → 35 %, 6m 05s");
		expect(eventDetail({ type: "filament_percent_level", subtype: "low", payload: { level: 95, lastPercentage: 64 } }))
			.toBe("95 % → 64 %");
		expect(eventDetail({ type: "filament_percent_drift", subtype: "low", payload: { level: 95, threshold: 6, firstLayer: 138,
			lastLayer: 194, layers: 57, extreme: 45.73 } })).toBe("95 % → 46 % (L138–194)");
		expect(eventDetail({ type: "unknown", subtype: null, payload: null })).toBe("");
	});
});

describe("charts", () => {
	it("puts time series on seconds since the job start", () => {
		const config = timeSeriesConfig([{ label: "T", unit: "°C", points: [[1000, 20], [6000, 25]] },
			{ label: "load", points: [[1000, 0.5]], secondary: true }], 1000, { markers: [{ ts: 3000, label: "pause" }] });
		expect(config.data.datasets[0].data).toEqual([{ x: 0, y: 20 }, { x: 5, y: 25 }]);
		expect(config.data.datasets[0].label).toBe("T (°C)");
		expect(config.data.datasets[1].yAxisID).toBe("y2");
		expect(config.options.scales.y2).toBeDefined();
		expect(config.options.plugins.qaMarkers.markers[0].x).toBe(2);
		expect(config.options.scales.x.ticks.callback(125)).toBe("2:05");
	});

	it("builds layer, histogram and trend configs", () => {
		const layers = layerChartConfig([1, 2], [{ label: "d", values: [10, null] }], { yMin: 0, yMax: 100 });
		expect(layers.data.labels).toEqual(["1", "2"]);
		expect(layers.options.scales.y.max).toBe(100);
		const hist = histogramConfig("x", [{ from: 98, to: 100, s: 5 }]);
		expect(hist.data.labels).toEqual(["98–100"]);
		const trend = trendConfig([{ label: "a", points: [{ ts: 0, value: 1, jobId: "j" }] }], 86_400_000);
		expect(trend.data.datasets[0].data[0]).toMatchObject({ x: -1, y: 1, jobId: "j" });
		expect(trend.options.scales.x).toMatchObject({ max: 0, suggestedMin: -7 });  // one job: a week up to now
	});

	it("draws markers inside the chart area only", () => {
		const ctx = { save: vi.fn(), restore: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn(), setLineDash: vi.fn() };
		const chart = { ctx, chartArea: { left: 0, right: 100, top: 0, bottom: 50 }, scales: { x: { getPixelForValue: (v) => v * 10 } } };
		markerPlugin.afterDatasetsDraw(chart, {}, { markers: [{ x: 5, label: "a" }, { x: 50, label: "outside" }] });
		expect(ctx.stroke).toHaveBeenCalledTimes(1);
	});
});

describe("LiveClient", () => {
	let sockets;

	class FakeSocket {
		constructor(url) {
			this.url = url;
			this.closed = false;
			this.sent = [];
			sockets.push(this);
		}
		send(text) {
			this.sent.push(text);
		}
		close() {
			this.closed = true;
		}
	}

	beforeEach(() => {
		sockets = [];
		vi.useFakeTimers();
	});
	afterEach(() => {
		vi.useRealTimers();
	});

	it("delivers frames and reconnects with backoff while polling", async () => {
		const frames = [];
		const states = [];
		const poll = vi.fn(() => Promise.resolve());
		const client = new LiveClient({
			url: () => "ws://h/machine/QualityAssurance/live",
			onFrame: (f) => frames.push(f),
			onState: (s) => states.push(s),
			poll,
			createSocket: (url) => new FakeSocket(url),
			minDelayMs: 100,
			pollIntervalMs: 1000
		});
		client.start();
		sockets[0].onopen();
		// the first frame carries the session to the daemon (DSF forwards it with client frames only)
		expect(sockets[0].sent).toEqual(["{\"type\":\"hello\"}"]);
		expect(client.state).toBe("connecting");
		sockets[0].onmessage({ data: "{\"type\":\"hello\",\"ts\":1}" });
		sockets[0].onmessage({ data: "not json" });
		expect(frames).toEqual([{ type: "hello", ts: 1 }]);
		expect(client.state).toBe("open");

		sockets[0].onclose();
		expect(client.state).toBe("polling");
		expect(poll).toHaveBeenCalledTimes(1);
		vi.advanceTimersByTime(100);
		expect(sockets).toHaveLength(2);
		sockets[1].onclose();
		vi.advanceTimersByTime(150);
		expect(sockets).toHaveLength(2);  // the delay doubled to 200 ms
		vi.advanceTimersByTime(60);
		expect(sockets).toHaveLength(3);
		vi.advanceTimersByTime(1000);
		expect(poll.mock.calls.length).toBeGreaterThanOrEqual(2);
		sockets[2].onopen();
		sockets[2].onmessage({ data: "{\"type\":\"hello\",\"ts\":2}" });
		const polled = poll.mock.calls.length;
		vi.advanceTimersByTime(5000);
		expect(poll.mock.calls.length).toBe(polled);  // polling stops once the socket is back

		client.stop();
		expect(sockets[2].closed).toBe(true);
		expect(states[states.length - 1]).toBe("closed");
	});

	it("keeps backing off while the daemon refuses the socket (no session)", () => {
		const client = new LiveClient({ url: () => "ws://h/live", onFrame: () => {}, createSocket: (u) => new FakeSocket(u), minDelayMs: 100 });
		client.start();
		for (const wait of [100, 200, 400]) {
			const socket = sockets[sockets.length - 1];
			socket.onopen();
			socket.onclose({ code: 1008 });  // refused before any frame
			const count = sockets.length;
			vi.advanceTimersByTime(wait - 1);
			expect(sockets).toHaveLength(count);
			vi.advanceTimersByTime(1);
			expect(sockets).toHaveLength(count + 1);
		}
		client.stop();
	});

	it("only polls without a socket URL", () => {
		const poll = vi.fn(() => Promise.resolve());
		const client = new LiveClient({ url: () => null, onFrame: () => {}, poll, createSocket: (u) => new FakeSocket(u), minDelayMs: 100 });
		client.start();
		expect(client.state).toBe("polling");
		expect(sockets).toHaveLength(0);
		client.stop();
	});
});
