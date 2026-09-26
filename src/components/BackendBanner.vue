<template>
	<v-alert v-if="running === false" type="warning" variant="tonal" density="compact" class="mb-3">
		<div class="d-flex align-center flex-wrap ga-2">
			<span class="flex-grow-1">{{ $t("plugins.QualityAssurance.backend.stopped") }}</span>
			<v-btn size="small" variant="flat" color="warning" :loading="starting" @click="start">
				{{ $t("plugins.QualityAssurance.backend.start") }}
			</v-btn>
		</div>
		<div v-if="error" class="text-caption mt-1">
			{{ $t("plugins.QualityAssurance.backend.startFailed", { error }) }}
		</div>
	</v-alert>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

import { isBackendRunning } from "../core/backend";
import type { HostAdapter } from "../core/host";

/** Warns while the SBC daemon does not run (pid ≤ 0) and offers to start it */
export default defineComponent({
	props: {
		host: { type: Object as PropType<HostAdapter>, required: true }
	},
	data() {
		return { starting: false, error: null as string | null };
	},
	computed: {
		running(): boolean | null {
			return isBackendRunning(this.host.pluginEntry());
		}
	},
	methods: {
		async start() {
			this.starting = true;
			this.error = null;
			try {
				await this.host.startBackend();
			} catch (e) {
				this.error = e instanceof Error ? e.message : String(e);
			} finally {
				this.starting = false;
			}
		}
	}
});
</script>
