import { mountInDwc } from "dwc-plugin-test-kit";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { spectrumConfig } from "../../src/core/charts";
import { expectNoVueWarnings } from "../helpers.js";

vi.mock("../../src/core/charts", async (importOriginal) => {
	const actual = await importOriginal();
	return {
		...actual,
		registerChartPlugins: () => {},
		Chart: class {
			constructor(canvas, config) { this.config = config; this.data = config.data; this.options = config.options; }
			update() {}
			destroy() {}
		}
	};
});

const SpectrumView = (await import("../../src/components/SpectrumView.vue")).default;
const SpectrumCompare = (await import("../../src/components/SpectrumCompare.vue")).default;
const TrendsView = (await import("../../src/components/TrendsView.vue")).default;

const JOB = { id: "j1", file: "0:/gcodes/a.gcode" };
const spectrum = (id, ts, axis, peak, extra = {}) => ({
	id, ts_ms: ts, layer: 2 + id, board: 60, axis, sampling_rate: 800, n_samples: 1000, freqs: [0.8, 1.6, 2.4],
	amplitudes: [0.01, peak / 100, 0.02], peak_hz: peak, rms: 0.05, source: "60.i2c.lis", ...extra
});
const SPECTRA = [spectrum(1, 1000, "X", 48), spectrum(2, 1000, "Y", 60), spectrum(3, 1000, "Z", 30),
	spectrum(4, 2000, "X", 50), spectrum(5, 2000, "Y", 61), spectrum(6, 2000, "Z", 31)];
const REFERENCES = {
	autoCount: 5,
	references: ["X", "Y", "Z"].map((axis) => ({ axis, mode: "auto", setAt: null, spectrumIds: [1], jobIds: ["j0"], complete: false,
		freqs: [0.8, 1.6, 2.4], amplitudes: [0.01, 0.4, 0.02], peakHz: 47, rms: 0.05 }))
};

let warn;
beforeEach(() => {
	warn = vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => {
	vi.restoreAllMocks();
});

const flush = async () => {
	for (let i = 0; i < 4; i++) {
		await new Promise((r) => setTimeout(r, 0));
	}
};

function api(overrides = {}) {
	return {
		spectra: vi.fn(async (id) => ({ jobId: id, spectra: SPECTRA })),
		references: vi.fn(async () => REFERENCES),
		setReference: vi.fn(async (axis, spectrumId) => ({
			autoCount: 5,
			references: REFERENCES.references.map((r) => (r.axis === axis && spectrumId !== null
				? { ...r, mode: "manual", spectrumIds: [spectrumId], jobIds: ["j1"], complete: true } : r))
		})),
		latestSpectra: vi.fn(async (axis) => ({ axis, spectra: [spectrum(9, 5000, axis, 49, { job_id: "j9", ts: "2026-09-27T10:00:00Z" })] })),
		trends: vi.fn(async (metric) => ({ metric, points: [
			{ jobId: "j1", ts: "2026-09-26T10:00:00Z", result: "completed", material: null, axis: "X", value: 48 },
			{ jobId: "j1", ts: "2026-09-26T10:00:00Z", result: "completed", material: null, axis: "Y", value: 60 }] })),
		...overrides
	};
}

describe("spectrumConfig", () => {
	it("draws amplitude over frequency, the reference dashed", () => {
		const config = spectrumConfig([{ label: "X", freqs: [1, 2], amplitudes: [0.1, 0.2] },
			{ label: "Reference", freqs: [1, 2], amplitudes: [0.1, 0.1], dashed: true }]);
		expect(config.data.datasets[0].data).toEqual([{ x: 1, y: 0.1 }, { x: 2, y: 0.2 }]);
		expect(config.data.datasets[1].borderDash).toEqual([6, 4]);
		expect(config.options.scales.x.title.text).toBe("Hz");
	});
});

describe("SpectrumView", () => {
	it("shows the newest recording against the reference and lists every recording", async () => {
		const a = api();
		const wrapper = mountInDwc(SpectrumView, { props: { api: a, job: JOB } });
		await flush();
		expect(a.spectra).toHaveBeenCalledWith("j1");
		expect(wrapper.vm.selected).toBe(2000);
		const datasets = wrapper.vm.config.data.datasets;
		expect(datasets).toHaveLength(2);
		expect(datasets[0].data[1].y).toBeCloseTo(0.5);    // recording 2, X
		expect(datasets[1].borderDash).toEqual([6, 4]);     // reference
		expect(wrapper.findAll("tbody tr")).toHaveLength(2);
		expect(wrapper.text()).toContain("plugins.QualityAssurance.spectra.referenceAutoPartial");
		wrapper.vm.axis = "Y";
		await flush();
		expect(wrapper.vm.config.data.datasets[0].data[1].y).toBeCloseTo(0.61);
		expectNoVueWarnings(warn);
	});

	it("makes a recording the axis' reference and goes back to automatic", async () => {
		const a = api();
		const wrapper = mountInDwc(SpectrumView, { props: { api: a, job: JOB } });
		await flush();
		await wrapper.vm.setReference(wrapper.vm.current.id);
		expect(a.setReference).toHaveBeenCalledWith("X", 4);
		expect(wrapper.vm.isReference).toBe(true);
		await flush();
		expect(wrapper.text()).toContain("plugins.QualityAssurance.spectra.referenceManual");
		await wrapper.vm.setReference(null);
		expect(a.setReference).toHaveBeenLastCalledWith("X", null);
		expectNoVueWarnings(warn);
	});

	it("says when a job has no spectra", async () => {
		const a = api({ spectra: vi.fn(async (id) => ({ jobId: id, spectra: [] })) });
		const wrapper = mountInDwc(SpectrumView, { props: { api: a, job: JOB } });
		await flush();
		expect(wrapper.text()).toContain("plugins.QualityAssurance.spectra.none");
		expectNoVueWarnings(warn);
	});
});

describe("SpectrumCompare and the trends", () => {
	it("overlays the last jobs' spectra and the reference per axis", async () => {
		const a = api();
		const wrapper = mountInDwc(SpectrumCompare, { props: { api: a } });
		await flush();
		expect(a.latestSpectra).toHaveBeenCalledWith("X", 5);
		expect(wrapper.vm.config.data.datasets.map((d) => d.label)).toEqual([expect.stringContaining("j9"),
			"plugins.QualityAssurance.spectra.reference"]);
		wrapper.vm.axis = "Z";
		await flush();
		expect(a.latestSpectra).toHaveBeenLastCalledWith("Z", 5);
		expectNoVueWarnings(warn);
	});

	it("groups the vibration trends by axis and shows the comparison", async () => {
		const a = api();
		const wrapper = mountInDwc(TrendsView, { props: { api: a } });
		await flush();
		expect(wrapper.findComponent(SpectrumCompare).exists()).toBe(false);
		wrapper.vm.metric = "spectrum_peak_hz";
		await flush();
		expect(a.trends).toHaveBeenLastCalledWith("spectrum_peak_hz", { limit: 200, material: undefined });
		expect(wrapper.vm.config.data.datasets.map((d) => d.label)).toEqual(["X", "Y"]);
		expect(wrapper.findComponent(SpectrumCompare).exists()).toBe(true);
		expectNoVueWarnings(warn);
	});
});
