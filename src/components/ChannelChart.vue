<template>
	<div>
		<div class="d-flex flex-wrap align-center ga-2 mb-2">
			<v-autocomplete v-model="selected" :items="available" :label="$t('plugins.QualityAssurance.channels.pick')"
				density="compact" variant="outlined" hide-details multiple chips closable-chips class="qa-channels" />
			<v-select v-model="resolution" :items="resolutions" :label="$t('plugins.QualityAssurance.channels.resolution')"
				density="compact" variant="outlined" hide-details class="qa-resolution" />
			<v-btn variant="text" size="small" prepend-icon="mdi-refresh" :loading="loading" @click="load">
				{{ $t("plugins.QualityAssurance.common.refresh") }}
			</v-btn>
		</div>
		<v-alert v-if="error" type="error" variant="tonal" density="compact" class="mb-2">{{ error }}</v-alert>
		<v-alert v-if="rawPruned" type="info" variant="tonal" density="compact" class="mb-2">
			{{ $t("plugins.QualityAssurance.channels.pruned") }}
		</v-alert>
		<chart-canvas :config="config" :height="320" />
		<div v-if="downsampled" class="text-caption text-medium-emphasis">{{ $t("plugins.QualityAssurance.channels.downsampled") }}</div>
	</div>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

import type { QaApi, QaEvent, Resolution, SamplesAnswer } from "../core/api";
import { timeSeriesConfig, type TimeSeries } from "../core/charts";
import { eventColor } from "../core/format";
import ChartCanvas from "./ChartCanvas.vue";

function unitOf(channel: string): string {
	if (channel.endsWith(".load") || channel.endsWith(".avgPwm") || channel.endsWith("Value") || channel.endsWith(".factor")) {
		return "0..1";
	}
	if (channel.endsWith(".current") || channel.endsWith(".active") || channel.endsWith(".standby") || channel.endsWith(".lastReading") || channel.endsWith(".mcuTemp")) {
		return "°C";
	}
	if (channel.includes("Percentage")) {
		return "%";
	}
	if (channel.endsWith(".vIn") || channel.endsWith(".v12")) {
		return "V";
	}
	return "";
}

/** Time series of chosen channels of a job, events as vertical markers */
export default defineComponent({
	components: { ChartCanvas },
	props: {
		api: { type: Object as PropType<QaApi>, required: true },
		jobId: { type: String, required: true },
		startMs: { type: Number, required: true },
		events: { type: Array as PropType<Array<QaEvent>>, default: () => [] },
		rawPruned: { type: Boolean, default: false },
		/** Channels shown first (the page derives them from the job's nozzle heaters) */
		defaultChannels: { type: Array as PropType<Array<string>>, default: () => [] },
		/** Bumped when the job may have new samples */
		reloadKey: { type: Number, default: 0 }
	},
	data() {
		return {
			available: [] as Array<string>,
			selected: [] as Array<string>,
			resolution: "auto" as Resolution,
			answer: null as SamplesAnswer | null,
			loading: false,
			error: null as string | null
		};
	},
	computed: {
		resolutions(): Array<{ title: string; value: Resolution }> {
			return (["auto", "coarse", "fine"] as Array<Resolution>).map((value) => ({ title: this.$t(`plugins.QualityAssurance.channels.${value}`), value }));
		},
		downsampled(): boolean {
			return this.answer?.downsampled ?? false;
		},
		config(): any {
			const channels = this.answer?.channels ?? {};
			const series: Array<TimeSeries> = Object.entries(channels).map(([name, points]) => ({
				label: name, unit: unitOf(name), points, secondary: unitOf(name) === "0..1",
				stepped: name.endsWith(".state") || name.endsWith(".status") || name.endsWith(".active")
			}));
			const markers = this.events
				.filter((e) => !["job_start", "job_end"].includes(e.type))
				.map((e) => ({ ts: e.ts_ms, label: e.type, color: this.markerColor(e.type) }));
			return timeSeriesConfig(series, this.startMs, { markers });
		}
	},
	watch: {
		jobId() {
			this.load();
		},
		reloadKey() {
			this.load();
		},
		selected() {
			this.load();
		},
		resolution() {
			this.load();
		}
	},
	async mounted() {
		try {
			const catalog = await this.api.channels();
			this.available = [...catalog.channels, ...catalog.derived].sort();
		} catch {
			this.available = [];
		}
		this.selected = this.defaultChannels.filter((c) => this.available.includes(c));
		if (this.selected.length === 0) {
			this.load();
		}
	},
	methods: {
		markerColor(type: string): string {
			return { error: "rgba(229,57,53,0.8)", warning: "rgba(251,140,0,0.8)", info: "rgba(30,136,229,0.6)" }[eventColor(type)] ?? "rgba(128,128,128,0.5)";
		},
		async load() {
			if (this.selected.length === 0) {
				this.answer = null;
				return;
			}
			this.loading = true;
			this.error = null;
			try {
				this.answer = await this.api.samples(this.jobId, this.selected, { resolution: this.resolution });
			} catch (e) {
				this.error = e instanceof Error ? e.message : String(e);
			} finally {
				this.loading = false;
			}
		}
	}
});
</script>

<style scoped>
.qa-channels {
	min-width: 280px;
	flex: 1 1 400px;
}
.qa-resolution {
	max-width: 280px;
}
</style>
