<template>
	<v-card variant="tonal" class="pa-3 mb-3">
		<div class="d-flex flex-wrap align-center ga-3">
			<v-icon :color="recording ? 'error' : 'grey'">{{ recording ? "mdi-record-circle" : "mdi-record-circle-outline" }}</v-icon>
			<div>
				<div class="text-subtitle-2">
					{{ recording ? $t("plugins.QualityAssurance.live.recording") : $t("plugins.QualityAssurance.live.idle") }}
				</div>
				<div class="text-caption text-medium-emphasis">
					<span v-if="jobId">{{ jobId }}</span>
					<span v-if="layer !== null"> · {{ $t("plugins.QualityAssurance.live.layer", { layer }) }}</span>
					<span> · {{ $t(`plugins.QualityAssurance.live.states.${connection}`) }}</span>
				</div>
			</div>
			<v-spacer />
			<div v-for="nozzle in loads" :key="nozzle.heater" class="qa-load">
				<div class="text-caption">{{ $t("plugins.QualityAssurance.live.load", { heater: nozzle.heater }) }}</div>
				<v-progress-linear :model-value="(nozzle.mean ?? 0) * 100" :color="levelColor(nozzle.level)" height="8" rounded />
				<div class="text-caption">{{ formatPercent(nozzle.mean) }}</div>
			</div>
			<div v-if="percentage !== null" class="qa-load">
				<div class="text-caption">{{ $t("plugins.QualityAssurance.live.monitor") }}</div>
				<div class="text-subtitle-2">{{ percentage }} %</div>
			</div>
		</div>
		<div v-if="lastEvent" class="text-caption mt-2">
			{{ $t("plugins.QualityAssurance.live.lastEvent") }}:
			<v-chip size="x-small" :color="eventColor(lastEvent.type)" variant="tonal">
				{{ $t(`plugins.QualityAssurance.eventTypes.${lastEvent.type}`) }}
			</v-chip>
			<span class="ml-1">{{ lastEvent.subtype }}</span>
		</div>
	</v-card>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

import type { LiveFrame, LiveState } from "../core/ws";
import { eventColor, formatPercent } from "../core/format";

/** What the daemon records right now, from the live frames the page hands in */
export default defineComponent({
	props: {
		frame: { type: Object as PropType<LiveFrame | null>, default: null },
		status: { type: Object as PropType<{ state: string; currentJobId: string | null; layer: number | null } | null>, default: null },
		connection: { type: String as PropType<LiveState>, default: "closed" }
	},
	data() {
		return {
			sample: null as LiveFrame | null,
			lastEvent: null as { type: string; subtype: string | null } | null
		};
	},
	computed: {
		recording(): boolean {
			return (this.sample?.jobId ?? this.status?.currentJobId ?? null) !== null && this.status?.state !== "idle";
		},
		jobId(): string | null {
			return this.sample?.jobId ?? this.status?.currentJobId ?? null;
		},
		layer(): number | null {
			return this.sample?.layer ?? this.status?.layer ?? null;
		},
		loads(): Array<{ heater: string; mean: number | null; level: string | null }> {
			const loads = (this.sample?.heaterLoad ?? {}) as Record<string, { mean: number; level: string | null }>;
			return Object.entries(loads).map(([heater, v]) => ({ heater, mean: v.mean, level: v.level }));
		},
		percentage(): number | null {
			const value = this.sample?.values?.["fm.0.lastPercentage"];
			return typeof value === "number" ? value : null;
		}
	},
	watch: {
		frame(frame: LiveFrame | null) {
			if (!frame) {
				return;
			}
			if (frame.type === "sample") {
				this.sample = frame;
			} else if (frame.type === "event") {
				this.lastEvent = frame.event;
			} else if (frame.type === "job" && frame.event === "end") {
				this.sample = null;
			}
		}
	},
	methods: {
		eventColor,
		formatPercent,
		levelColor(level: string | null): string {
			return level === "limit" ? "error" : level === "high" ? "warning" : "primary";
		}
	}
});
</script>

<style scoped>
.qa-load {
	min-width: 120px;
}
</style>
