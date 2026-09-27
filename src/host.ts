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
			// Same composition as the REST connector's own object-model socket, session key in the
			// query (@duet3d/connectors 3.7.0-rc.2 RestConnector.connect; `sessionKey` is a private
			// field there). DSF looks the key up for custom WebSocket endpoints but does not check
			// it; the daemon refuses a socket without a session (qa_api.make_live_handler)
			const protocol = connector.settings?.protocol === "https:" ? "wss:" : "ws:";
			const key = connector.sessionKey ? `?sessionKey=${encodeURIComponent(connector.sessionKey)}` : "";
			return `${protocol}//${connector.hostname}${connector.settings?.baseURL ?? "/"}${path}${key}`;
		}
	};
}
