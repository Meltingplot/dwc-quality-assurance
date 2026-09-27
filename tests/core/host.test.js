import { resetDwc, setConnected } from "dwc-plugin-test-kit";
import { afterEach, describe, expect, it } from "vitest";

import { createHost } from "../../src/host";

afterEach(() => {
	resetDwc();
});

describe("host adapter", () => {
	it("puts the session key into the live socket URL (DSF forwards it, the daemon checks it)", () => {
		setConnected(true);
		expect(createHost().webSocketUrl("machine/QualityAssurance/live"))
			.toBe("ws://printer.local/machine/QualityAssurance/live?sessionKey=0f3c9a7e5b2d4e81a6c0d9b7e4f21a35");
	});

	it("has no socket URL while not connected", () => {
		setConnected(false);
		expect(createHost().webSocketUrl("machine/QualityAssurance/live")).toBeNull();
	});
});
