<template>
	<div class="qa-chart" :style="{ height: `${height}px` }">
		<canvas v-show="hasData" ref="canvas" />
		<div v-if="!hasData" class="qa-chart__empty text-caption text-medium-emphasis">
			{{ emptyText || $t("plugins.QualityAssurance.common.noData") }}
		</div>
	</div>
</template>

<script lang="ts">
import { defineComponent, markRaw, type PropType } from "vue";

import { applyConfig, Chart, registerChartPlugins } from "../core/charts";

/** Draws a Chart.js config: created once, updated in place, destroyed on unmount */
export default defineComponent({
	props: {
		config: { type: Object as PropType<{ type: string; data: any; options: any } | null>, default: null },
		height: { type: Number, default: 260 },
		emptyText: { type: String, default: "" }
	},
	data() {
		return { chart: null as any };
	},
	computed: {
		hasData(): boolean {
			const datasets = this.config?.data?.datasets ?? [];
			return datasets.some((d: { data?: Array<unknown> }) => (d.data?.length ?? 0) > 0);
		}
	},
	watch: {
		config() {
			this.draw();
		}
	},
	mounted() {
		this.draw();
	},
	beforeUnmount() {
		this.chart?.destroy();
		this.chart = null;
	},
	methods: {
		draw() {
			if (!this.config || !this.hasData) {
				return;
			}
			if (this.chart && this.chart.config?.type === this.config.type) {
				applyConfig(this.chart, this.config);
				return;
			}
			this.chart?.destroy();
			registerChartPlugins();
			this.chart = markRaw(new Chart(this.$refs.canvas as HTMLCanvasElement, this.config as any));
		}
	}
});
</script>

<style scoped>
.qa-chart {
	position: relative;
	width: 100%;
}
.qa-chart__empty {
	position: absolute;
	inset: 0;
	display: flex;
	align-items: center;
	justify-content: center;
}
</style>
