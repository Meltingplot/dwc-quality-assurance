<template>
	<v-card variant="flat">
		<div class="d-flex flex-wrap align-center ga-2 mb-2">
			<v-select v-model="result" :items="resultItems" :label="$t('plugins.QualityAssurance.jobs.result')" density="compact"
				variant="outlined" hide-details clearable class="qa-filter" />
			<v-text-field v-model="material" :label="$t('plugins.QualityAssurance.jobs.material')" density="compact"
				variant="outlined" hide-details clearable class="qa-filter" @keyup.enter="load" @click:clear="clearMaterial" />
			<v-spacer />
			<v-btn variant="text" size="small" prepend-icon="mdi-refresh" :loading="loading" @click="load">
				{{ $t("plugins.QualityAssurance.common.refresh") }}
			</v-btn>
		</div>
		<v-alert v-if="error" type="error" variant="tonal" density="compact" class="mb-2">{{ error }}</v-alert>
		<v-table density="compact" hover>
			<thead>
				<tr>
					<th>{{ $t("plugins.QualityAssurance.jobs.file") }}</th>
					<th>{{ $t("plugins.QualityAssurance.jobs.started") }}</th>
					<th>{{ $t("plugins.QualityAssurance.jobs.duration") }}</th>
					<th>{{ $t("plugins.QualityAssurance.jobs.result") }}</th>
					<th>{{ $t("plugins.QualityAssurance.jobs.material") }}</th>
					<th class="text-right">{{ $t("plugins.QualityAssurance.jobs.ratio") }}</th>
					<th class="text-right">{{ $t("plugins.QualityAssurance.jobs.load") }}</th>
					<th class="text-right">{{ $t("plugins.QualityAssurance.jobs.events") }}</th>
				</tr>
			</thead>
			<tbody>
				<tr v-for="job in jobs" :key="job.id" class="qa-row" :class="{ 'qa-row--selected': job.id === selected }"
					@click="$emit('select', job.id)">
					<td>
						<div class="text-body-2">{{ fileName(job.file) }}</div>
						<div class="text-caption text-medium-emphasis">
							{{ job.id }}
							<v-chip v-if="job.partial" size="x-small" variant="outlined" class="ml-1">
								{{ $t("plugins.QualityAssurance.jobs.partial") }}
							</v-chip>
						</div>
					</td>
					<td class="text-no-wrap">{{ formatDateTime(job.startedAt) }}</td>
					<td class="text-no-wrap">{{ formatDuration(job.printTimeS) }}</td>
					<td>
						<v-chip size="small" :color="resultColor(job.result)" variant="tonal">
							{{ $t(`plugins.QualityAssurance.results.${job.qaResult}`) }}
						</v-chip>
					</td>
					<td>{{ job.material || "—" }}</td>
					<td class="text-right">{{ ratio(job) }}</td>
					<td class="text-right">{{ heaterLoad(job) }}</td>
					<td class="text-right">
						<v-chip v-if="countedEvents(job.events)" size="small" variant="tonal" :color="eventsColor(job)" :title="eventsTitle(job)">
							{{ countedEvents(job.events) }}
						</v-chip>
						<span v-else :title="eventsTitle(job)">{{ job.events ? 0 : "—" }}</span>
					</td>
				</tr>
				<tr v-if="!loading && jobs.length === 0">
					<td colspan="8" class="text-center text-medium-emphasis">{{ $t("plugins.QualityAssurance.jobs.none") }}</td>
				</tr>
			</tbody>
		</v-table>
		<div v-if="pages > 1" class="d-flex justify-center mt-2">
			<v-pagination v-model="page" :length="pages" density="compact" :total-visible="7" />
		</div>
	</v-card>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

import type { JobEntry, QaApi } from "../core/api";
import { countedEvents, fileName, formatDateTime, formatDuration, formatPercent, levelColor, resultColor } from "../core/format";

const PAGE_SIZE = 25;

export default defineComponent({
	props: {
		api: { type: Object as PropType<QaApi>, required: true },
		selected: { type: String as PropType<string | null>, default: null },
		/** Bumped by the page when a job starts or ends */
		reloadKey: { type: Number, default: 0 }
	},
	emits: ["select"],
	data() {
		return {
			jobs: [] as Array<JobEntry>,
			total: 0,
			page: 1,
			result: null as string | null,
			material: "" as string | null,
			loading: false,
			error: null as string | null
		};
	},
	computed: {
		pages(): number {
			return Math.ceil(this.total / PAGE_SIZE);
		},
		resultItems(): Array<{ title: string; value: string }> {
			return ["finished", "cancelled", "aborted", "running", "unknown"].map((value) => ({
				title: this.$t(`plugins.QualityAssurance.results.${value === "finished" ? "completed" : value}`),
				value
			}));
		}
	},
	watch: {
		page() {
			this.load();
		},
		result() {
			this.page = 1;
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
		formatDuration,
		resultColor,
		async load() {
			this.loading = true;
			this.error = null;
			try {
				const answer = await this.api.jobs({
					limit: PAGE_SIZE,
					offset: (this.page - 1) * PAGE_SIZE,
					result: this.result ?? undefined,
					material: this.material || undefined
				});
				this.jobs = answer.jobs;
				this.total = answer.total;
			} catch (e) {
				this.error = e instanceof Error ? e.message : String(e);
			} finally {
				this.loading = false;
			}
		},
		clearMaterial() {
			this.material = "";
			this.load();
		},
		ratio(job: JobEntry): string {
			const values = Object.values(job.summary?.filamentRatio ?? {});
			return values.length ? formatPercent(values[0], 1) : "—";
		},
		heaterLoad(job: JobEntry): string {
			const values = Object.values(job.summary?.heaterLoadMean ?? {});
			return values.length ? formatPercent(Math.max(...values)) : "—";
		},
		countedEvents,
		/** Red with an error, orange with warnings only */
		eventsColor(job: JobEntry): string {
			return levelColor(job.events?.error ? "error" : "warning");
		},
		eventsTitle(job: JobEntry): string | undefined {
			const levels = job.events;
			return levels ? (["error", "warning", "info"] as const)
				.map((level) => `${levels[level]} ${this.$t(`plugins.QualityAssurance.levels.${level}`)}`).join(", ") : undefined;
		}
	}
});
</script>

<style scoped>
.qa-filter {
	max-width: 220px;
}
.qa-row {
	cursor: pointer;
}
.qa-row--selected {
	background: rgba(var(--v-theme-primary), 0.08);
}
</style>
