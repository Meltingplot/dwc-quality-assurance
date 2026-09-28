import { registerEmbeddableComponent, registerPluginMessages, registerRoute, unregisterEmbeddableComponent, unregisterRoute } from "@/plugins";
import Events from "@/utils/events";

import { ensureBackendRunning, PLUGIN_ID } from "./core/backend";
import LayerReplay from "./components/LayerReplay.vue";
import LayerTimelapse from "./components/LayerTimelapse.vue";
import { createHost } from "./host";
import de from "./i18n/de.json";
import en from "./i18n/en.json";
import QualityAssurance from "./QualityAssurance.vue";

const ROUTE_PATH = "/Plugins/QualityAssurance";

// Translations live under plugins.QualityAssurance.*
registerPluginMessages(PLUGIN_ID, { en, de });

registerRoute(QualityAssurance, {
	Plugins: {
		QualityAssurance: {
			icon: "mdi-clipboard-check-outline",
			caption: "plugins.QualityAssurance.menuCaption",
			path: ROUTE_PATH
		}
	}
});

// One layer's replay and camera frame for other plugins' pages: the CHX 350 analysis renders them
// by id with the props `jobId` (QA job id) and `layer` (job.layer numbering); docs/chx-integration.md §4.
// DWC 3.7's registry for widgets of flexible layouts (DuetWebControl v3.7-dev, src/plugins/index.ts,
// 2026-09-28); without props they follow the current or last job
const EMBEDDABLES = [
	{ id: `${PLUGIN_ID}.LayerReplay`, caption: "plugins.QualityAssurance.embed.layerReplay", icon: "mdi-layers-outline", component: LayerReplay },
	{ id: `${PLUGIN_ID}.LayerTimelapse`, caption: "plugins.QualityAssurance.embed.layerTimelapse", icon: "mdi-filmstrip", component: LayerTimelapse }
];
for (const embeddable of EMBEDDABLES) {
	registerEmbeddableComponent({ ...embeddable, pluginId: PLUGIN_ID, machineMode: "fff" });
}

// Start the daemon if DSF left it stopped (plugin upgrade, first boot of an image)
ensureBackendRunning(createHost());

function onPluginUnloaded(id: string) {
	if (id === PLUGIN_ID) {
		unregisterRoute(ROUTE_PATH);
		for (const embeddable of EMBEDDABLES) {
			unregisterEmbeddableComponent(embeddable.id);
		}
		Events.off("dwcPluginUnloaded", onPluginUnloaded);
	}
}
Events.on("dwcPluginUnloaded", onPluginUnloaded);
