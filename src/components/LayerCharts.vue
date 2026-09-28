<template>
	<div>
		<v-alert v-if="!answer || answer.layers.length === 0" type="info" variant="tonal" density="compact">
			{{ $t("plugins.QualityAssurance.layers.none") }}
		</v-alert>
		<template v-else>
			<div class="d-flex flex-wrap align-center ga-2 mb-2">
				<v-btn-toggle v-model="view" density="compact" variant="outlined" divided mandatory>
					<v-btn v-for="v in views" :key="v" :value="v" size="small">{{ $t(`plugins.QualityAssurance.layers.${v}`) }}</v-btn>
				</v-btn-toggle>
			</div>
			<chart-canvas :config="config" :height="280" />
			<div v-if="view === 'filament'" class="text-caption text-medium-emphasis mt-1">
				{{ $t("plugins.QualityAssurance.layers.filamentHint") }}
			</div>
			<div v-if="view === 'gearPasses'" class="text-caption text-medium-emphasis mt-1">
				{{ $t("plugins.QualityAssurance.layers.gearPassesHint") }}
			</div>
		</template>
	</div>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

import type { LayersAnswer } from "../core/api";
import { layerChartConfig, type LayerSeries } from "../core/charts";
import ChartCanvas from "./ChartCanvas.vue";

const VIEWS = ["duration", "temperatures", "heaterLoad", "monitor", "filament", "flow", "gearPasses"] as const;
type View = typeof VIEWS[number];

/** Per-layer charts of a job, including the histogram measured vs. commanded (PLAN.md §3) */
export default defineComponent({
	components: { ChartCanvas },
	props: {
		answer: { type: Object as PropType<LayersAnswer | null>, default: null }
	},
	data() {
		return { view: "duration" as View, views: VIEWS };
	},
	computed: {
		numbers(): Array<number> {
			return this.answer?.layers.map((l) => l.layer) ?? [];
		},
		config(): any {
			const layers = this.answer?.layers ?? [];
			const meta = this.answer?.meta;
			const t = (key: string) => this.$t(`plugins.QualityAssurance.layers.${key}`);
			let series: Array<LayerSeries> = [];
			let options: { yMin?: number; yMax?: number } = {};
			switch (this.view) {
				case "duration":
					series = [{ label: t("duration"), unit: "s", values: layers.map((l) => l.durationS) }];
					break;
				case "temperatures": {
					const sensors = meta?.sensors ?? [];
					series = sensors.map((s) => ({
						label: s.name || `#${s.index}`, unit: "°C", type: "line" as const,
						values: layers.map((l) => l.temps?.sensors?.[String(s.index)]?.mean ?? null)
					}));
					break;
				}
				case "heaterLoad": {
					const nozzles = (meta?.heaters ?? []).filter((h) => h.role === "nozzle");
					series = nozzles.flatMap((h) => [
						{ label: `${t("loadMean")} T${h.tool ?? h.index}`, unit: "%", type: "line" as const,
							values: layers.map((l) => { const s = l.loadStats?.[String(h.index)]; return s ? s.mean * 100 : null; }) },
						{ label: `${t("loadShareHigh")} T${h.tool ?? h.index}`, unit: "%",
							values: layers.map((l) => { const s = l.loadStats?.[String(h.index)]; return s ? s.shareHigh * 100 : null; }) }
					]);
					options = { yMin: 0, yMax: 100 };
					break;
				}
				case "monitor": {
					const keys = Object.keys(layers[layers.length - 1]?.fmStats ?? {});
					series = keys.flatMap((k) => [
						{ label: `${t("percentMean")} #${k}`, unit: "%", type: "line" as const, values: layers.map((l) => l.fmStats?.[k]?.mean ?? null) },
						{ label: `${t("percentMin")} #${k}`, unit: "%", type: "line" as const, values: layers.map((l) => l.fmStats?.[k]?.min ?? null) },
						{ label: `${t("percentMax")} #${k}`, unit: "%", type: "line" as const, values: layers.map((l) => l.fmStats?.[k]?.max ?? null) }
					]);
					break;
				}
				case "filament": {
					const keys = Object.keys(layers[layers.length - 1]?.filament ?? {});
					series = keys.flatMap((k) => [
						{ label: `${t("commanded")} #${k}`, unit: "mm", values: layers.map((l) => l.filament?.[k]?.commandedMm ?? null) },
						{ label: `${t("measured")} #${k}`, unit: "mm", values: layers.map((l) => l.filament?.[k]?.measuredMm ?? null) }
					]);
					break;
				}
				case "flow": {
					const keys = Object.keys(layers[layers.length - 1]?.flow ?? {});
					series = keys.map((k) => ({ label: `${t("flow")} #${k}`, unit: "mm³/s", values: layers.map((l) => l.flow?.[k] ?? null) }));
					break;
				}
				case "gearPasses":
					series = [{ label: t("gearPasses"), unit: "×", values: layers.map((l) => l.filamentPath?.gearPasses ?? null) }];
					options = { yMin: 0 };
					break;
			}
			// a sensor or monitor without a single value in this job is left out
			return layerChartConfig(this.numbers, series.filter((s) => s.values.some((v) => v !== null && v !== undefined)), options);
		}
	}
});
</script>
