<template>
	<div>
		<div class="d-flex flex-wrap align-center ga-2 mb-2">
			<div class="text-subtitle-2">{{ $t("plugins.QualityAssurance.spectra.compare") }}</div>
			<v-btn-toggle v-model="axis" mandatory density="compact" variant="outlined" divided>
				<v-btn v-for="a in axes" :key="a" :value="a" size="small">{{ a }}</v-btn>
			</v-btn-toggle>
		</div>
		<v-alert v-if="error" type="error" variant="tonal" density="compact" class="mb-2">{{ error }}</v-alert>
		<div v-if="!loading && !spectra.length" class="text-caption text-medium-emphasis">{{ $t("plugins.QualityAssurance.spectra.noneYet") }}</div>
		<chart-canvas v-else :config="config" :height="280" />
		<div v-if="hasFans" class="text-caption text-medium-emphasis mt-1">{{ $t("plugins.QualityAssurance.spectra.fansNote") }}</div>
	</div>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

import type { Axis, QaApi, ReferenceSpectrum, Spectrum } from "../core/api";
import { color, spectrumConfig, type SpectrumLine, type SpectrumSeries } from "../core/charts";
import { turningFans } from "../core/fans";
import { formatDateTime } from "../core/format";
import ChartCanvas from "./ChartCanvas.vue";

const JOBS = 5;

/**
 * Spectrum comparison over jobs: the newest spectrum of each of the last jobs against the reference,
 * with each spectrum's fan lines (rpm / 60) in its colour, named at the newest one
 */
export default defineComponent({
	components: { ChartCanvas },
	props: {
		api: { type: Object as PropType<QaApi>, required: true }
	},
	data() {
		return {
			axes: ["X", "Y", "Z"] as Array<Axis>,
			axis: "X" as Axis,
			spectra: [] as Array<Spectrum>,
			reference: null as ReferenceSpectrum | null,
			loading: false,
			error: null as string | null,
			request: 0
		};
	},
	computed: {
		hasFans(): boolean {
			return this.spectra.some((s) => turningFans(s).length > 0);
		},
		config(): any {
			const series: Array<SpectrumSeries> = this.spectra.map((s) => ({
				label: `${s.job_id ?? ""} ${formatDateTime(s.ts ?? new Date(s.ts_ms).toISOString())}`.trim(),
				freqs: s.freqs,
				amplitudes: s.amplitudes
			}));
			if (this.reference?.freqs.length) {
				series.push({ label: this.$t("plugins.QualityAssurance.spectra.reference"), freqs: this.reference.freqs,
					amplitudes: this.reference.amplitudes, dashed: true });
			}
			const lines: Array<SpectrumLine> = this.spectra.flatMap((s, i) => turningFans(s).map((f) => ({
				hz: f.hz,
				color: color(i),
				text: i === 0 ? this.$t("plugins.QualityAssurance.spectra.fanLine", {
					name: f.name || this.$t("plugins.QualityAssurance.spectra.fan", { fan: f.fan }), rpm: f.rpm }) : undefined
			})));
			return spectrumConfig(series, "g", lines);
		}
	},
	watch: {
		axis() {
			this.load();
		}
	},
	mounted() {
		this.load();
	},
	methods: {
		async load() {
			const request = ++this.request;
			this.loading = true;
			this.error = null;
			try {
				const [latest, references] = await Promise.all([this.api.latestSpectra(this.axis, JOBS), this.api.references()]);
				if (request === this.request) {
					this.spectra = latest.spectra;
					this.reference = references.references.find((r) => r.axis === this.axis) ?? null;
				}
			} catch (e) {
				if (request === this.request) {
					this.error = e instanceof Error ? e.message : String(e);
				}
			} finally {
				if (request === this.request) {
					this.loading = false;
				}
			}
		}
	}
});
</script>
