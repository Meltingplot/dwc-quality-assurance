/**
 * Stand-in for DWC 3.7's `@/plugins` under vitest.
 *
 * dwc-plugin-test-kit's stub has no-op registrations but no embeddable-component registry
 * (registerEmbeddableComponent, DWC 3.7). QA registers its layer views there, so this stub
 * records them for the test of src/index.ts.
 */
export * from "../../node_modules/dwc-plugin-test-kit/src/stubs/plugins.ts";

const embeddables = [];

/** Embeddable components registered and not unregistered since the last reset */
export function registeredEmbeddables() {
	return embeddables;
}

export function registerEmbeddableComponent(definition) {
	if (!embeddables.some((e) => e.id === definition.id)) {
		embeddables.push(definition);
	}
}

export function unregisterEmbeddableComponent(id) {
	const index = embeddables.findIndex((e) => e.id === id);
	if (index !== -1) {
		embeddables.splice(index, 1);
	}
}
