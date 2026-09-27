/**
 * The seam between QA's framework-neutral code and DuetWebControl 3.7.
 *
 * Everything the UI needs from DWC goes through these members; nothing in `core/` imports
 * a store. `src/host.ts` implements them on top of DWC's Pinia machine store, tests pass a
 * fake. Deliberately raw: plain reads and promises, no Vue types.
 */
export interface PluginEntry {
	id?: string;
	/** DSF: -1 stopped, 0 shutting down, > 0 running */
	pid?: number;
	data?: Map<string, unknown> | Record<string, unknown>;
}

export interface HostAdapter {
	/**
	 * Live read of this plugin's object-model entry. MUST read through the store on every
	 * call (a cached entry freezes every computed that depends on it). `null` = the model has
	 * no entry yet, `undefined` = no object model at all (not connected, tests)
	 */
	pluginEntry(): PluginEntry | null | undefined;
	/** Ask DSF to start this plugin's SBC daemon */
	startBackend(): Promise<void>;
	/**
	 * HTTP request against DSF (DWC's REST connector: base URL and session key are handled
	 * there). `path` is relative to the web root, e.g. `machine/QualityAssurance/status`
	 */
	request(method: string, path: string, params?: Record<string, string | number | boolean> | null,
		responseType?: XMLHttpRequestResponseType, body?: unknown, timeout?: number): Promise<any>;
	/**
	 * Absolute ws:// or wss:// URL for a path relative to the web root, with the session key as
	 * `sessionKey` query; null when not connected in SBC mode
	 */
	webSocketUrl(path: string): string | null;
}
