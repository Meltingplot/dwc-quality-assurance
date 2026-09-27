/**
 * Stand-in for DWC 3.7's `@/stores/machine` under vitest.
 *
 * dwc-plugin-test-kit's stub covers model/isConnected/sendCode/getFileList. QA also uses
 * startSbcPlugin (backend recovery), request (daemon HTTP API) and the connector (WebSocket
 * URL). The kit's shared state (`dwc.model`, fed by `setModel(...)`) is kept.
 */
import { dwc } from "dwc-plugin-test-kit";

const started = [];
const requests = [];
let startFailure = null;
let requestHandler = null;

/** SBC plugin IDs the code under test asked DSF to start, in order */
export function startedSbcPlugins() {
	return started;
}

/** HTTP requests made through the machine store, in order */
export function sentRequests() {
	return requests;
}

/** Make the next startSbcPlugin call reject, as DSF does when it refuses */
export function failNextSbcPluginStart(error) {
	startFailure = error;
}

/** Answer requests: `handler(method, path, params, body)` returns the response or throws */
export function setRequestHandler(handler) {
	requestHandler = handler;
}

/** Clear everything recorded here; call alongside the kit's resetDwc() */
export function resetMachineStub() {
	started.length = 0;
	requests.length = 0;
	startFailure = null;
	requestHandler = null;
}

export function useMachineStore() {
	return {
		get model() { return dwc.model; },
		get isConnected() { return dwc.connected; },
		get isSbcMode() { return dwc.connected; },
		get connector() {
			// sessionKey: a private field of @duet3d/connectors' RestConnector, DSF issues it as a GUID in "N" format
			return dwc.connected ? { hostname: "printer.local", settings: { protocol: "http:", baseURL: "/" }, sessionKey: "0f3c9a7e5b2d4e81a6c0d9b7e4f21a35" } : null;
		},
		async startSbcPlugin(plugin) {
			if (startFailure) {
				const error = startFailure;
				startFailure = null;
				throw error;
			}
			started.push(plugin);
		},
		async request(method, path, params = null, responseType = "json", body = null) {
			requests.push({ method, path, params, responseType, body });
			if (!requestHandler) {
				throw new Error(`no request handler for ${method} ${path}`);
			}
			return requestHandler(method, path, params, body);
		}
	};
}
