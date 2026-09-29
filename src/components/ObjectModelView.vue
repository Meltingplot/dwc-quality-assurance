<template>
	<div>
		<div v-if="!journal" class="text-medium-emphasis">{{ $t("plugins.QualityAssurance.om.none") }}</div>
		<template v-else>
			<div class="d-flex flex-wrap align-center ga-2">
				<v-text-field v-model="path" :label="$t('plugins.QualityAssurance.om.path')" placeholder="move.compensation"
					density="compact" variant="outlined" hide-details clearable class="qa-om-path" @keyup.enter="show" />
				<v-btn color="primary" variant="tonal" size="small" :loading="busy === 'state'" @click="show">
					{{ $t("plugins.QualityAssurance.om.show") }}
				</v-btn>
				<v-btn variant="tonal" size="small" :disabled="!path" :loading="busy === 'history'" @click="loadHistory">
					{{ $t("plugins.QualityAssurance.om.history") }}
				</v-btn>
				<v-spacer />
				<v-btn variant="text" size="small" prepend-icon="mdi-download" :loading="busy === 'download'" @click="download">
					{{ $t("plugins.QualityAssurance.om.download", { size: formatBytes(journal.bytes) }) }}
				</v-btn>
			</div>
			<v-slider v-model="at" :min="journal.start" :max="journal.end" :step="1000" density="compact" hide-details
				color="primary" class="mt-2" @end="show">
				<template #append>
					<span class="text-caption text-no-wrap">{{ clock(at) }}</span>
				</template>
			</v-slider>
			<v-alert v-if="error" type="error" variant="tonal" density="compact" class="my-2">{{ error }}</v-alert>

			<template v-if="points">
				<div class="text-caption text-medium-emphasis mb-1">
					{{ $t("plugins.QualityAssurance.om.changes", { n: points.length }) }}
					<template v-if="truncated"> · {{ $t("plugins.QualityAssurance.om.truncated") }}</template>
				</div>
				<v-table density="compact">
					<thead>
						<tr>
							<th>{{ $t("plugins.QualityAssurance.om.time") }}</th>
							<th>{{ $t("plugins.QualityAssurance.om.value") }}</th>
						</tr>
					</thead>
					<tbody>
						<tr v-for="(point, i) in points" :key="i" class="qa-om-point" @click="at = point.t; show()">
							<td class="text-no-wrap">{{ clock(point.t) }}</td>
							<td class="qa-om-value">{{ plain(point.value) }}</td>
						</tr>
					</tbody>
				</v-table>
			</template>
			<template v-else-if="state">
				<div class="text-caption text-medium-emphasis mb-1">
					{{ state.snapshotAt !== null
						? $t("plugins.QualityAssurance.om.replayed", { time: clock(state.at), snapshot: clock(state.snapshotAt) })
						: $t("plugins.QualityAssurance.om.noSnapshot", { time: clock(state.at) }) }}
				</div>
				<context-table v-if="isObject(state.value)" :data="state.value" />
				<div v-else class="qa-om-value">{{ plain(state.value) }}</div>
			</template>
		</template>
	</div>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

import type { JobDetail, ObjectModelAnswer, QaApi } from "../core/api";
import { formatBytes, formatClock } from "../core/format";
import ContextTable from "./ContextTable.vue";

/**
 * The object model of a job as it was at any time, and every change of one value, replayed from the
 * journal (qa_journal.py): for questions nobody thought of while the job ran
 */
export default defineComponent({
	components: { ContextTable },
	props: {
		api: { type: Object as PropType<QaApi>, required: true },
		job: { type: Object as PropType<JobDetail>, required: true }
	},
	data() {
		return {
			path: "",
			at: 0,
			state: null as ObjectModelAnswer | null,
			points: null as Array<{ t: number; value: unknown }> | null,
			truncated: false,
			busy: null as string | null,
			error: null as string | null
		};
	},
	computed: {
		journal() {
			return this.job.journal ?? null;
		},
		startMs(): number {
			return new Date(this.job.startedAt).getTime();
		}
	},
	watch: {
		journal: {
			handler(journal) {
				this.at = journal ? journal.end : 0;
			},
			immediate: true
		}
	},
	methods: {
		formatBytes,
		clock(ms: number): string {
			return formatClock((ms - this.startMs) / 1000);
		},
		isObject(value: unknown): value is Record<string, unknown> {
			return value !== null && typeof value === "object";
		},
		plain(value: unknown): string {
			return value === null || value === undefined ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value);
		},
		async run(kind: string, action: () => Promise<void>) {
			this.busy = kind;
			this.error = null;
			try {
				await action();
			} catch (e) {
				this.error = e instanceof Error ? e.message : String(e);
			} finally {
				this.busy = null;
			}
		},
		show() {
			return this.run("state", async () => {
				this.state = await this.api.objectModel(this.job.id, this.at, (this.path ?? "").trim());
				this.points = null;
			});
		},
		loadHistory() {
			return this.run("history", async () => {
				const answer = await this.api.objectModelHistory(this.job.id, (this.path ?? "").trim());
				this.points = answer.points;
				this.truncated = answer.truncated;
			});
		},
		download() {
			return this.run("download", async () => {
				const blob = await this.api.journalBlob(this.job.id);
				const url = URL.createObjectURL(new Blob([blob], { type: "application/gzip" }));
				const link = document.createElement("a");
				link.href = url;
				link.download = `qa-${this.job.id}-om.jsonl.gz`;
				link.click();
				setTimeout(() => URL.revokeObjectURL(url), 1000);
			});
		}
	}
});
</script>

<style scoped>
.qa-om-path {
	min-width: 240px;
	max-width: 480px;
}

.qa-om-value {
	font-family: monospace;
	word-break: break-all;
}

.qa-om-point {
	cursor: pointer;
}
</style>
