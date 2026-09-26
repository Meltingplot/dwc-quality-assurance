import { mountInDwc } from "dwc-plugin-test-kit";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import BackendBanner from "../../src/components/BackendBanner.vue";
import { expectNoVueWarnings } from "../helpers.js";

function host(pid, start = () => Promise.resolve()) {
	return {
		pluginEntry: () => (pid === undefined ? null : { pid }),
		startBackend: vi.fn(start),
		request: () => Promise.reject(new Error("unused")),
		webSocketUrl: () => null
	};
}

let warn;
beforeEach(() => {
	warn = vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => {
	vi.restoreAllMocks();
});

describe("BackendBanner", () => {
	it("stays hidden while the daemon runs", () => {
		const wrapper = mountInDwc(BackendBanner, { props: { host: host(42) } });
		expect(wrapper.text()).not.toContain("backend.stopped");
		expectNoVueWarnings(warn);
	});

	it("stays hidden while the state is unknown", () => {
		const wrapper = mountInDwc(BackendBanner, { props: { host: host(undefined) } });
		expect(wrapper.text()).toBe("");
	});

	it("offers to start a stopped daemon", async () => {
		const h = host(-1);
		const wrapper = mountInDwc(BackendBanner, { props: { host: h } });
		expect(wrapper.text()).toContain("plugins.QualityAssurance.backend.stopped");
		await wrapper.find("button").trigger("click");
		expect(h.startBackend).toHaveBeenCalledTimes(1);
		expectNoVueWarnings(warn);
	});

	it("shows why a start failed", async () => {
		const h = host(-1, () => Promise.reject(new Error("refused")));
		const wrapper = mountInDwc(BackendBanner, { props: { host: h } });
		await wrapper.find("button").trigger("click");
		await new Promise((r) => setTimeout(r, 0));
		expect(wrapper.text()).toContain("backend.startFailed");
	});
});
