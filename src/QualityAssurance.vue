<template>
	<div class="qa-page pa-2">
		<backend-banner :host="host" />
		<h1 class="text-h6 mb-2">{{ $t("plugins.QualityAssurance.title") }}</h1>
	</div>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

import BackendBanner from "./components/BackendBanner.vue";
import type { HostAdapter } from "./core/host";
import { createHost } from "./host";

/** QA page of the classic DWC: job list, job detail, replay, timelapse, trends, settings */
export default defineComponent({
	components: { BackendBanner },
	props: {
		host: { type: Object as PropType<HostAdapter>, default: () => createHost() }
	}
});
</script>
