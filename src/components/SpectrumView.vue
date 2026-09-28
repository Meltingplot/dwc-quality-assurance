<template>
	<div>
		<v-alert v-if="error" type="error" variant="tonal" density="compact" class="mb-2">{{ error }}</v-alert>
		<v-progress-linear v-if="loading && !recordings.length" indeterminate color="primary" class="mb-2" />
		<div v-else-if="!recordings.length" class="text-caption text-medium-emphasis">{{ $t("plugins.QualityAssurance.spectra.none") }}</div>
		<template v-else>
			<div class="d-flex flex-wrap align-center ga-2 mb-2">
				<v-btn-toggle v-model="axis" mandatory density="compact" variant="outlined" divided>
					<v-btn v-for="a in axes" :key="a" :value="a" size="small">{{ a }}</v-btn>
				</v-btn-toggle>
				<v-select v-model="selected" :items="recordingItems" :label="$t('plugins.QualityAssurance.spectra.recording')"
					density="compact" variant="outlined" hide-details class="qa-recording" />
				<v-spacer />
				<v-btn size="small" variant="text" prepend-icon="mdi-pin-outline" :disabled="!current || isReference" :loading="saving"
					@click="setReference(current ? current.id : null)">
					{{ $t("plugins.QualityAssurance.spectra.useAsReference", { axis }) }}
				</v-btn>
				<v-btn v-if="reference?.mode === 'manual'" size="small" variant="text" :loading="saving" @click="setReference(null)">
					{{ $t("plugins.QualityAssurance.spectra.autoReference") }}
				</v-btn>
			</div>
			<chart-canvas :config="config" :height="300" />
			<div class="text-caption text-medium-emphasis mt-1">{{ referenceText }}</div>
			<v-table density="compact" class="mt-2">
				<thead>
					<tr>
						<th>{{ $t("plugins.QualityAssurance.spectra.time") }}</th>
						<th>{{ $t("plugins.QualityAssurance.spectra.layer") }}</th>
						<th v-for="a in axes" :key="a">{{ $t("plugins.QualityAssurance.spectra.peakRms", { axis: a }) }}</th>
					</tr>
				</thead>
				<tbody>
					<tr v-for="r in recordings" :key="r.ts" :class="{ 'qa-selected': r.ts === selected }" class="qa-row" @click="selected = r.ts">
						<td>{{ formatDateTime(new Date(r.ts).toISOString()) }}</td>
						<td>{{ r.layer ?? "—" }}</td>
						<td v-for="a in axes" :key="a">
							<template v-if="r.byAxis[a]">{{ formatNumber(r.byAxis[a].peak_hz, 1) }} Hz · {{ formatNumber(r.byAxis[a].rms, 4) }} g</template>
							<template v-else>—</template>
						</td>
					</tr>
				</tbody>
			</v-table>
		</template>
	</div>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

import type { Axis, JobDetail, QaApi, ReferenceSpectrum, Spectrum } from "../core/api";
import { spectrumConfig, type SpectrumSeries } from "../core/charts";
import { formatDateTime, formatNumber } from "../core/format";
import ChartCanvas from "./ChartCanvas.vue";

interface Recording {
	ts: number;
	layer: number | null;
	byAxis: Partial<Record<Axis, Spectrum>>;
}

/**
 * The job's vibration spectra (accelerometer, PLAN.md §3): one recording every intervalMin with a
 * spectrum per axis, drawn against the axis' reference; a recording can become the reference.
 */
export default defineComponent({
	components: { ChartCanvas },
	props: {
		api: { type: Object as PropType<QaApi>, required: true },
		job: { type: Object as PropType<JobDetail>, required: true },
		/** Bumped when the job may have new recordings */
		reloadKey: { type: Number, default: 0 }
	},
	data() {
		return {
			spectra: [] as Array<Spectrum>,
			references: [] as Array<ReferenceSpectrum>,
			autoCount: 5,
			axis: "X" as Axis,
			selected: null as number | null,
			loading: false,
			saving: false,
			error: null as string | null
		};
	},
	computed: {
		recordings(): Array<Recording> {
			const map = new Map<number, Recording>();
			for (const s of this.spectra) {
				if (!map.has(s.ts_ms)) {
					map.set(s.ts_ms, { ts: s.ts_ms, layer: s.layer, byAxis: {} });
				}
				map.get(s.ts_ms)!.byAxis[s.axis] = s;
			}
			return [...map.values()].sort((a, b) => a.ts - b.ts);
		},
		axes(): Array<Axis> {
			return (["X", "Y", "Z"] as Array<Axis>).filter((a) => this.spectra.some((s) => s.axis === a));
		},
		recordingItems(): Array<{ title: string; value: number }> {
			return this.recordings.map((r) => ({
				title: `${formatDateTime(new Date(r.ts).toISOString())}${r.layer !== null ? ` · ${this.$t("plugins.QualityAssurance.spectra.layer")} ${r.layer}` : ""}`,
				value: r.ts
			}));
		},
		current(): Spectrum | null {
			return this.recordings.find((r) => r.ts === this.selected)?.byAxis[this.axis] ?? null;
		},
		reference(): ReferenceSpectrum | null {
			return this.references.find((r) => r.axis === this.axis) ?? null;
		},
		isReference(): boolean {
			return this.reference?.mode === "manual" && this.current !== null && this.reference.spectrumIds.includes(this.current.id);
		},
		referenceText(): string {
			const ref = this.reference;
			if (!ref || !ref.freqs.length) {
				return this.$t("plugins.QualityAssurance.spectra.referenceNone");
			}
			return ref.mode === "manual"
				? this.$t("plugins.QualityAssurance.spectra.referenceManual", { jobs: ref.jobIds.join(", ") })
				: this.$t(ref.complete ? "plugins.QualityAssurance.spectra.referenceAuto" : "plugins.QualityAssurance.spectra.referenceAutoPartial",
					{ count: ref.spectrumIds.length, total: this.autoCount });
		},
		config(): any {
			const series: Array<SpectrumSeries> = [];
			if (this.current) {
				series.push({ label: `${this.axis} ${formatDateTime(new Date(this.current.ts_ms).toISOString())}`, freqs: this.current.freqs,
					amplitudes: this.current.amplitudes });
			}
			if (this.reference?.freqs.length) {
				series.push({ label: this.$t("plugins.QualityAssurance.spectra.reference"), freqs: this.reference.freqs,
					amplitudes: this.reference.amplitudes, dashed: true });
			}
			return spectrumConfig(series);
		}
	},
	watch: {
		"job.id"() {
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
		formatDateTime,
		formatNumber,
		async load() {
			this.loading = true;
			this.error = null;
			try {
				const [spectra, references] = await Promise.all([this.api.spectra(this.job.id), this.api.references()]);
				this.spectra = spectra.spectra;
				this.references = references.references;
				this.autoCount = references.autoCount;
				if (!this.recordings.some((r) => r.ts === this.selected)) {
					this.selected = this.recordings.length ? this.recordings[this.recordings.length - 1].ts : null;
				}
				if (this.axes.length && !this.axes.includes(this.axis)) {
					this.axis = this.axes[0];
				}
			} catch (e) {
				this.error = e instanceof Error ? e.message : String(e);
			} finally {
				this.loading = false;
			}
		},
		async setReference(spectrumId: number | null) {
			this.saving = true;
			try {
				const answer = await this.api.setReference(this.axis, spectrumId);
				this.references = answer.references;
				this.autoCount = answer.autoCount;
			} catch (e) {
				this.error = e instanceof Error ? e.message : String(e);
			} finally {
				this.saving = false;
			}
		}
	}
});
</script>

<style scoped>
.qa-recording {
	max-width: 320px;
}
.qa-row {
	cursor: pointer;
}
.qa-selected {
	background: rgba(var(--v-theme-primary), 0.08);
}
</style>
