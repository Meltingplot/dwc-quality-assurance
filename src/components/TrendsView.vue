<template>
	<div>
		<div class="d-flex flex-wrap align-center ga-2 mb-2">
			<v-select v-model="metric" :items="metrics" :label="$t('plugins.QualityAssurance.trends.metric')" density="compact"
				variant="outlined" hide-details class="qa-metric" />
			<v-text-field v-model="material" :label="$t('plugins.QualityAssurance.jobs.material')" density="compact"
				variant="outlined" hide-details clearable class="qa-metric" @keyup.enter="load" @click:clear="clearMaterial" />
			<v-btn variant="text" size="small" prepend-icon="mdi-refresh" :loading="loading" @click="load">
				{{ $t("plugins.QualityAssurance.common.refresh") }}
			</v-btn>
		</div>
		<v-alert v-if="error" type="error" variant="tonal" density="compact" class="mb-2">{{ error }}</v-alert>
		<chart-canvas :config="config" :height="320" />
		<div class="text-caption text-medium-emphasis mt-1">{{ $t(`plugins.QualityAssurance.trends.hint.${metric}`) }}</div>
		<spectrum-compare v-if="metric.startsWith('spectrum_') || metric === 'fan_amplitude'" :api="api" class="mt-4" />
	</div>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

import type { QaApi, TrendMetric, TrendPoint } from "../core/api";
import { trendConfig, type TrendSeries } from "../core/charts";
import ChartCanvas from "./ChartCanvas.vue";
import SpectrumCompare from "./SpectrumCompare.vue";

const METRICS: Array<TrendMetric> = ["heater_load_mean", "fm_avg_percentage", "filament_ratio", "mm_per_rev",
	"esteps_suggested", "heat_up_s", "duration_s", "events", "spectrum_peak_hz", "spectrum_rms", "fan_rpm", "fan_amplitude"];

/** Key a point belongs to: the plan groups the heater load by heater, setpoint and nozzle (§5.4.1) */
function groupKey(metric: TrendMetric, p: TrendPoint): string {
	switch (metric) {
		case "heater_load_mean":
			return `T${p.tool ?? p.heater} ${p.setpoint} °C${p.nozzleDiameter ? ` ⌀${p.nozzleDiameter}` : ""}`;
		case "fm_avg_percentage":
		case "filament_ratio":
		case "mm_per_rev":
			return `#${p.monitor}`;
		case "heat_up_s":
			return `H${p.heater} ${p.setpoint} °C`;
		case "spectrum_peak_hz":
		case "spectrum_rms":
			return String(p.axis);
		case "fan_rpm":
		case "fan_amplitude":
			return `#${p.fan}${p.name ? ` ${p.name}` : ""}`;
		default:
			return "";
	}
}

export default defineComponent({
	components: { ChartCanvas, SpectrumCompare },
	props: {
		api: { type: Object as PropType<QaApi>, required: true }
	},
	data() {
		return {
			metric: "heater_load_mean" as TrendMetric,
			material: "" as string | null,
			points: [] as Array<TrendPoint>,
			loading: false,
			error: null as string | null
		};
	},
	computed: {
		metrics(): Array<{ title: string; value: TrendMetric }> {
			return METRICS.map((value) => ({ title: this.$t(`plugins.QualityAssurance.trends.metrics.${value}`), value }));
		},
		config(): any {
			const groups = new Map<string, TrendSeries>();
			const scale = this.metric === "heater_load_mean" || this.metric === "filament_ratio" ? 100 : 1;
			for (const p of [...this.points].reverse()) {
				const key = groupKey(this.metric, p) || this.$t(`plugins.QualityAssurance.trends.metrics.${this.metric}`);
				if (!groups.has(key)) {
					groups.set(key, { label: key, points: [] });
				}
				groups.get(key)!.points.push({ ts: new Date(p.ts).getTime(), value: Math.round(p.value * scale * 1e4) / 1e4, jobId: p.jobId });
			}
			const series = [...groups.values()];
			if (this.metric.startsWith("spectrum_") || this.metric.startsWith("fan_")) {
				series.sort((a, b) => a.label.localeCompare(b.label));  // X, Y, Z; #0, #1, …
			}
			return trendConfig(series, Date.now(), this.$t(`plugins.QualityAssurance.trends.units.${this.metric}`));
		}
	},
	watch: {
		metric() {
			this.load();
		}
	},
	mounted() {
		this.load();
	},
	methods: {
		async load() {
			this.loading = true;
			this.error = null;
			try {
				this.points = (await this.api.trends(this.metric, { limit: 200, material: this.material || undefined })).points;
			} catch (e) {
				this.error = e instanceof Error ? e.message : String(e);
			} finally {
				this.loading = false;
			}
		},
		clearMaterial() {
			this.material = "";
			this.load();
		}
	}
});
</script>

<style scoped>
.qa-metric {
	max-width: 280px;
}
</style>
