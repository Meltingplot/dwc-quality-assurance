import { useMachineStore } from "@/stores/machine";

import { getPluginEntry, PLUGIN_ID } from "./core/backend";
import type { HostAdapter } from "./core/host";

/**
 * {@link HostAdapter} on DWC 3.7's Pinia machine store. The store is resolved on every call:
 * safe at plugin load and inside components, and it keeps the reads reactive.
 * `@/stores/machine` is externalised by DWC's plugin builder, so this is DWC's own store.
 */
export function createHost(): HostAdapter {
	const machine = () => useMachineStore();
	return {
		pluginEntry: () => getPluginEntry(machine().model),
		startBackend: () => Promise.resolve(machine().startSbcPlugin(PLUGIN_ID)),
		request: (method, path, params = null, responseType = "json", body = null, timeout) =>
			machine().request(method, path, params, responseType, body, timeout),
		webSocketUrl(path: string): string | null {
			const connector = machine().connector as any;
			if (!machine().isSbcMode || !connector) {
				return null;
			}
			// Same composition as the REST connector's own object-model socket
			// (@duet3d/connectors RestConnector.connect). DSF does not require a session key on
			// custom WebSocket endpoints (CustomEndpointMiddleware only looks one up)
			const protocol = connector.settings?.protocol === "https:" ? "wss:" : "ws:";
			return `${protocol}//${connector.hostname}${connector.settings?.baseURL ?? "/"}${path}`;
		}
	};
}
