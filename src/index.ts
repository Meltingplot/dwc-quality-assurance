import { registerPluginMessages, registerRoute, unregisterRoute } from "@/plugins";
import Events from "@/utils/events";

import { ensureBackendRunning, PLUGIN_ID } from "./core/backend";
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

// Start the daemon if DSF left it stopped (plugin upgrade, first boot of an image)
ensureBackendRunning(createHost());

function onPluginUnloaded(id: string) {
	if (id === PLUGIN_ID) {
		unregisterRoute(ROUTE_PATH);
		Events.off("dwcPluginUnloaded", onPluginUnloaded);
	}
}
Events.on("dwcPluginUnloaded", onPluginUnloaded);
