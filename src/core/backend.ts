import type { HostAdapter, PluginEntry } from "./host";

/**
 * Keeping the SBC daemon running.
 *
 * Installing a plugin over an existing version leaves the daemon stopped ("partially
 * started" in DWC): DSF's InstallPlugin runs UninstallPlugin { ForUpgrade } first, which
 * stops the process, then re-registers the plugin with pid -1 and never starts it. On the
 * CHX 350 image the plugin comes with the image instead, but a fresh image boots DSF before
 * the plugin list is complete as well. `ensureBackendRunning` waits for the plugin's entry
 * and asks DSF to start it when the pid says it does not run. A crash later on is covered
 * by the manifest's `sbcAutoRestart`.
 *
 * Whether the daemon runs is read from the pid only: DSF keeps a crashed daemon's HTTP
 * endpoints registered, so a 404 or an unanswered endpoint is no signal
 * (DuetSoftwareFramework v3.7-dev, verified 2026-09-26).
 */

export const PLUGIN_ID = "QualityAssurance";

const DEFAULT_INTERVAL_MS = 1500;
const DEFAULT_MAX_ATTEMPTS = 20;

/**
 * This plugin's entry in a machine object model. `model.plugins` is a Map keyed by plugin
 * id in DWC 3.7; tests may pass a plain object
 */
export function getPluginEntry(model: any): PluginEntry | null | undefined {
	if (!model) {
		return undefined;
	}
	const plugins = model.plugins;
	if (!plugins) {
		return null;
	}
	const plugin = plugins instanceof Map ? plugins.get(PLUGIN_ID) : plugins[PLUGIN_ID];
	return plugin ?? null;
}

/**
 * A value of this plugin's data in the object model (plugin.json ``data``, written by the
 * daemon); ``data`` is a Map in DWC 3.7, a plain object in tests
 */
export function pluginData(entry: PluginEntry | null | undefined, key: string): unknown {
	const data = entry?.data;
	if (!data) {
		return undefined;
	}
	return data instanceof Map ? data.get(key) : data[key];
}

/** true/false from the pid, null while unknown */
export function isBackendRunning(entry: PluginEntry | null | undefined): boolean | null {
	if (!entry || typeof entry.pid !== "number") {
		return null;
	}
	return entry.pid > 0;
}

export interface EnsureOptions {
	intervalMs?: number;
	maxAttempts?: number;
}

/**
 * Start the daemon if the object model reports it stopped. Polls until the plugin entry
 * shows up, gives up at once without an object model. Resolves whether a start was issued
 */
export function ensureBackendRunning(host: HostAdapter, options: EnsureOptions = {}): Promise<boolean> {
	const intervalMs = options.intervalMs ?? DEFAULT_INTERVAL_MS;
	const maxAttempts = options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS;

	return new Promise((resolve) => {
		let attempts = 0;
		const check = () => {
			attempts++;
			let entry: PluginEntry | null | undefined;
			try {
				entry = host.pluginEntry();
			} catch {
				entry = undefined;
			}
			if (entry === undefined) {
				resolve(false);
				return;
			}

			const running = isBackendRunning(entry);
			if (running === true) {
				resolve(false);
				return;
			}
			if (running === false) {
				host.startBackend().then(
					() => resolve(true),
					(e) => {
						// eslint-disable-next-line no-console
						console.warn(`[${PLUGIN_ID}] could not start the SBC backend`, e);
						resolve(false);
					}
				);
				return;
			}
			if (attempts >= maxAttempts) {
				resolve(false);
				return;
			}
			setTimeout(check, intervalMs);
		};
		check();
	});
}
