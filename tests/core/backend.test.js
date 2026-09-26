import { describe, expect, it, vi } from "vitest";

import { ensureBackendRunning, getPluginEntry, isBackendRunning, PLUGIN_ID } from "../../src/core/backend";

function fakeHost(entries, { startFails = false } = {}) {
	const calls = { start: 0 };
	let i = 0;
	return {
		calls,
		pluginEntry: () => (i < entries.length ? entries[i++] : entries[entries.length - 1]),
		startBackend: () => {
			calls.start++;
			return startFails ? Promise.reject(new Error("refused")) : Promise.resolve();
		},
		request: () => Promise.reject(new Error("unused")),
		webSocketUrl: () => null
	};
}

describe("getPluginEntry", () => {
	it("reads the entry from a Map", () => {
		const entry = { id: PLUGIN_ID, pid: 42 };
		expect(getPluginEntry({ plugins: new Map([[PLUGIN_ID, entry]]) })).toBe(entry);
	});

	it("reads the entry from a plain object", () => {
		const entry = { id: PLUGIN_ID, pid: 42 };
		expect(getPluginEntry({ plugins: { [PLUGIN_ID]: entry } })).toBe(entry);
	});

	it("tells a missing entry from a missing model", () => {
		expect(getPluginEntry({ plugins: new Map() })).toBeNull();
		expect(getPluginEntry(null)).toBeUndefined();
	});
});

describe("isBackendRunning", () => {
	it("maps the pid", () => {
		expect(isBackendRunning({ pid: 123 })).toBe(true);
		expect(isBackendRunning({ pid: -1 })).toBe(false);
		expect(isBackendRunning({ pid: 0 })).toBe(false);
		expect(isBackendRunning({})).toBeNull();
		expect(isBackendRunning(null)).toBeNull();
	});
});

describe("ensureBackendRunning", () => {
	it("starts a stopped daemon", async () => {
		const host = fakeHost([{ pid: -1 }]);
		await expect(ensureBackendRunning(host)).resolves.toBe(true);
		expect(host.calls.start).toBe(1);
	});

	it("leaves a running daemon alone", async () => {
		const host = fakeHost([{ pid: 7 }]);
		await expect(ensureBackendRunning(host)).resolves.toBe(false);
		expect(host.calls.start).toBe(0);
	});

	it("waits for the plugin entry", async () => {
		const host = fakeHost([null, null, { pid: -1 }]);
		await expect(ensureBackendRunning(host, { intervalMs: 1 })).resolves.toBe(true);
	});

	it("gives up without an object model", async () => {
		const host = fakeHost([undefined]);
		await expect(ensureBackendRunning(host)).resolves.toBe(false);
		expect(host.calls.start).toBe(0);
	});

	it("gives up after the attempts", async () => {
		const host = fakeHost([null]);
		await expect(ensureBackendRunning(host, { intervalMs: 1, maxAttempts: 3 })).resolves.toBe(false);
	});

	it("survives a refused start", async () => {
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		const host = fakeHost([{ pid: -1 }], { startFails: true });
		await expect(ensureBackendRunning(host)).resolves.toBe(false);
		expect(warn).toHaveBeenCalled();
		warn.mockRestore();
	});
});
