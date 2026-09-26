import { expect } from "vitest";

/**
 * Components mount against real Vuetify 4: a wrong prop or component name is valid markup
 * that only the running component notices. Fail on every Vue warning it produced.
 */
export function expectNoVueWarnings(warnSpy) {
	const messages = warnSpy.mock.calls.map((args) => String(args[0]));
	expect(messages.filter((m) => /Failed to resolve|\[Vue warn\]/.test(m))).toEqual([]);
}
