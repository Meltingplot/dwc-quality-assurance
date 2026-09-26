<template>
	<div>
		<v-alert v-if="error" type="error" variant="tonal" density="compact" class="mb-2">{{ error }}</v-alert>
		<v-progress-linear v-if="loading" indeterminate color="primary" class="mb-2" />
		<template v-if="job">
			<div class="d-flex flex-wrap align-center ga-2 mb-3">
				<div>
					<div class="text-h6">{{ fileName(job.file) }}</div>
					<div class="text-caption text-medium-emphasis">
						{{ job.id }} · {{ formatDateTime(job.startedAt) }} – {{ formatDateTime(job.endedAt) }}
					</div>
				</div>
				<v-chip :color="resultColor(job.result)" variant="tonal">{{ $t(`plugins.QualityAssurance.results.${job.qaResult}`) }}</v-chip>
				<v-chip v-if="job.partial" variant="outlined" size="small">{{ $t("plugins.QualityAssurance.jobs.partialFrom", { layer: job.startLayer ?? "?" }) }}</v-chip>
				<v-spacer />
				<v-btn variant="text" size="small" prepend-icon="mdi-download" :loading="exporting" @click="download">
					{{ $t("plugins.QualityAssurance.job.export") }}
				</v-btn>
			</div>

			<v-row density="compact" class="mb-2">
				<v-col v-for="tile in tiles" :key="tile.key" cols="6" sm="4" md="2">
					<v-card variant="tonal" class="pa-2">
						<div class="text-caption text-medium-emphasis">{{ $t(`plugins.QualityAssurance.job.${tile.key}`) }}</div>
						<div class="text-subtitle-1">{{ tile.value }}</div>
					</v-card>
				</v-col>
			</v-row>

			<v-tabs v-model="tab" density="compact" class="mb-2">
				<v-tab value="layers">{{ $t("plugins.QualityAssurance.job.tabLayers") }}</v-tab>
				<v-tab value="channels">{{ $t("plugins.QualityAssurance.job.tabChannels") }}</v-tab>
				<v-tab value="events">{{ $t("plugins.QualityAssurance.job.tabEvents") }} ({{ events.length }})</v-tab>
				<v-tab value="summary">{{ $t("plugins.QualityAssurance.job.tabSummary") }}</v-tab>
				<v-tab value="context">{{ $t("plugins.QualityAssurance.job.tabContext") }}</v-tab>
			</v-tabs>
			<v-window v-model="tab">
				<v-window-item value="layers">
					<layer-charts :answer="layers" />
					<div v-if="distribution.length" class="mt-4">
						<div class="text-subtitle-2 mb-1">{{ $t("plugins.QualityAssurance.job.distribution") }}</div>
						<chart-canvas :config="distributionConfig" :height="200" />
					</div>
				</v-window-item>
				<v-window-item value="channels">
					<channel-chart v-if="tab === 'channels'" :api="api" :job-id="job.id" :start-ms="startMs" :events="events"
						:raw-pruned="job.rawPruned" :default-channels="defaultChannels" />
				</v-window-item>
				<v-window-item value="events">
					<event-list :events="events" :start-ms="startMs" />
				</v-window-item>
				<v-window-item value="summary">
					<context-table :data="job.summary" />
				</v-window-item>
				<v-window-item value="context">
					<context-table :data="job.context" />
				</v-window-item>
			</v-window>
		</template>
	</div>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

import type { JobDetail, LayersAnswer, QaApi, QaEvent } from "../core/api";
import { histogramConfig } from "../core/charts";
import { fileName, formatDateTime, formatDuration, formatPercent, resultColor } from "../core/format";
import ChannelChart from "./ChannelChart.vue";
import ChartCanvas from "./ChartCanvas.vue";
import ContextTable from "./ContextTable.vue";
import EventList from "./EventList.vue";
import LayerCharts from "./LayerCharts.vue";

const QUIET_EVENTS = new Set(["job_start", "job_end", "setpoint_change", "pause", "resume", "babystep", "daemon_started_mid_job"]);

export default defineComponent({
	components: { ChannelChart, ChartCanvas, ContextTable, EventList, LayerCharts },
	props: {
		api: { type: Object as PropType<QaApi>, required: true },
		jobId: { type: String, required: true },
		/** Bumped by the page when new data for this job may exist */
		reloadKey: { type: Number, default: 0 }
	},
	data() {
		return {
			job: null as JobDetail | null,
			layers: null as LayersAnswer | null,
			events: [] as Array<QaEvent>,
			tab: "layers",
			loading: false,
			exporting: false,
			error: null as string | null
		};
	},
	computed: {
		startMs(): number {
			return this.job ? new Date(this.job.startedAt).getTime() : 0;
		},
		defaultChannels(): Array<string> {
			const heaters = (this.job?.context?.heaters ?? []) as Array<{ index: number; role: string }>;
			const nozzles = heaters.filter((h) => h.role === "nozzle").map((h) => h.index);
			return [...nozzles.flatMap((i) => [`heater.${i}.current`, `heater.${i}.load`]), "fm.0.lastPercentage"];
		},
		distribution(): Array<{ from: number; to: number; s: number }> {
			const filament = (this.job?.summary?.filament ?? {}) as Record<string, { percentDistribution?: Array<{ from: number; to: number; s: number }> }>;
			const first = Object.values(filament)[0];
			return first?.percentDistribution ?? [];
		},
		distributionConfig(): any {
			return histogramConfig(this.$t("plugins.QualityAssurance.job.distribution"), this.distribution);
		},
		tiles(): Array<{ key: string; value: string }> {
			const summary = (this.job?.summary ?? {}) as Record<string, any>;
			const filament = Object.values(summary.filament ?? {})[0] as Record<string, any> | undefined;
			const loads = Object.values(summary.thermal?.heaterLoad ?? {}).filter((l: any) => l?.nozzle) as Array<Record<string, any>>;
			const events = Object.entries(summary.events ?? {}).filter(([type]) => !QUIET_EVENTS.has(type))
				.reduce((sum, [, e]) => sum + ((e as { count: number }).count ?? 0), 0);
			return [
				{ key: "duration", value: formatDuration(this.job?.durationS) },
				{ key: "layers", value: String(this.layers?.layers.length ?? "—") },
				{ key: "ratio", value: formatPercent(filament?.ratio, 1) },
				{ key: "avgPercentage", value: filament?.avgPercentage !== undefined && filament?.avgPercentage !== null ? `${filament.avgPercentage} %` : "—" },
				{ key: "loadMean", value: loads.length ? formatPercent(Math.max(...loads.map((l) => l.mean ?? 0))) : "—" },
				{ key: "events", value: this.job?.summary ? String(events) : "—" }
			];
		}
	},
	watch: {
		jobId() {
			this.load();
		},
		reloadKey() {
			this.load();
		}
	},
	mounted() {
		this.load();
	},
	methods: {
		fileName,
		formatDateTime,
		resultColor,
		async load() {
			this.loading = true;
			this.error = null;
			try {
				const [job, layers, events] = await Promise.all([
					this.api.job(this.jobId), this.api.layers(this.jobId), this.api.events(this.jobId)
				]);
				this.job = job;
				this.layers = layers;
				this.events = events.events;
			} catch (e) {
				this.error = e instanceof Error ? e.message : String(e);
			} finally {
				this.loading = false;
			}
		},
		async download() {
			if (!this.job) {
				return;
			}
			this.exporting = true;
			try {
				const blob = await this.api.exportBlob(this.job.id);
				const url = URL.createObjectURL(new Blob([blob], { type: "application/json" }));
				const link = document.createElement("a");
				link.href = url;
				link.download = `qa-${this.job.id}.json`;
				link.click();
				setTimeout(() => URL.revokeObjectURL(url), 1000);
			} catch (e) {
				this.error = e instanceof Error ? e.message : String(e);
			} finally {
				this.exporting = false;
			}
		}
	}
});
</script>
