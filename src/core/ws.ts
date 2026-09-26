/**
 * Client of the daemon's `live` WebSocket (docs/api.md): reconnects with a growing delay and,
 * while the socket is down, polls `status` every 5 s so the page still shows whether a job
 * records (PLAN.md §5.8).
 */

export interface LiveFrame {
	type: "hello" | "sample" | "event" | "layer" | "job" | "status" | "ping";
	ts: number;
	[key: string]: any;
}

export interface LiveOptions {
	/** URL of the socket, null when there is none (not connected in SBC mode) */
	url: () => string | null;
	onFrame: (frame: LiveFrame) => void;
	onState?: (state: LiveState) => void;
	/** Fallback while the socket is down */
	poll?: () => Promise<void>;
	/** Injected for tests */
	createSocket?: (url: string) => WebSocket;
	minDelayMs?: number;
	maxDelayMs?: number;
	pollIntervalMs?: number;
}

export type LiveState = "connecting" | "open" | "polling" | "closed";

export class LiveClient {
	private socket: WebSocket | null = null;
	private delay: number;
	private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	private pollTimer: ReturnType<typeof setInterval> | null = null;
	private stopped = true;
	state: LiveState = "closed";

	constructor(private readonly options: LiveOptions) {
		this.delay = options.minDelayMs ?? 1000;
	}

	start() {
		if (!this.stopped) {
			return;
		}
		this.stopped = false;
		this.connect();
	}

	stop() {
		this.stopped = true;
		if (this.reconnectTimer !== null) {
			clearTimeout(this.reconnectTimer);
			this.reconnectTimer = null;
		}
		this.stopPolling();
		if (this.socket !== null) {
			const socket = this.socket;
			this.socket = null;
			socket.onclose = null;
			socket.close();
		}
		this.setState("closed");
	}

	private setState(state: LiveState) {
		this.state = state;
		this.options.onState?.(state);
	}

	private connect() {
		const url = this.options.url();
		if (url === null) {
			this.fallback();
			return;
		}
		this.setState("connecting");
		let socket: WebSocket;
		try {
			socket = this.options.createSocket ? this.options.createSocket(url) : new WebSocket(url);
		} catch {
			this.fallback();
			return;
		}
		this.socket = socket;
		socket.onopen = () => {
			this.delay = this.options.minDelayMs ?? 1000;
			this.stopPolling();
			this.setState("open");
		};
		socket.onmessage = (event: MessageEvent) => {
			try {
				this.options.onFrame(JSON.parse(String(event.data)) as LiveFrame);
			} catch {
				// not JSON: ignore
			}
		};
		socket.onclose = () => {
			this.socket = null;
			this.fallback();
		};
		socket.onerror = () => {
			// onclose follows
		};
	}

	private fallback() {
		if (this.stopped) {
			return;
		}
		this.startPolling();
		this.reconnectTimer = setTimeout(() => {
			this.reconnectTimer = null;
			if (!this.stopped) {
				this.connect();
			}
		}, this.delay);
		this.delay = Math.min(this.delay * 2, this.options.maxDelayMs ?? 30000);
	}

	private startPolling() {
		if (!this.options.poll || this.pollTimer !== null) {
			this.setState("polling");
			return;
		}
		this.setState("polling");
		const poll = this.options.poll;
		poll().catch(() => undefined);
		this.pollTimer = setInterval(() => {
			poll().catch(() => undefined);
		}, this.options.pollIntervalMs ?? 5000);
	}

	private stopPolling() {
		if (this.pollTimer !== null) {
			clearInterval(this.pollTimer);
			this.pollTimer = null;
		}
	}
}
