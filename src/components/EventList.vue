<template>
	<div>
		<div class="d-flex flex-wrap ga-1 mb-2">
			<v-chip v-for="type in types" :key="type" size="small" :color="eventColor(type)"
				:variant="hidden.includes(type) ? 'outlined' : 'tonal'" @click="toggle(type)">
				{{ $t(`plugins.QualityAssurance.eventTypes.${type}`) }} ({{ counts[type] }})
			</v-chip>
		</div>
		<v-table density="compact">
			<thead>
				<tr>
					<th>{{ $t("plugins.QualityAssurance.events.time") }}</th>
					<th>{{ $t("plugins.QualityAssurance.events.layer") }}</th>
					<th>{{ $t("plugins.QualityAssurance.events.type") }}</th>
					<th>{{ $t("plugins.QualityAssurance.events.detail") }}</th>
					<th>{{ $t("plugins.QualityAssurance.events.position") }}</th>
				</tr>
			</thead>
			<tbody>
				<tr v-for="event in visible" :key="event.id" class="qa-event" @click="$emit('select', event)">
					<td class="text-no-wrap">{{ clock(event.ts_ms) }}</td>
					<td>{{ event.layer ?? "—" }}</td>
					<td>
						<v-chip size="x-small" :color="eventColor(event.type)" variant="tonal">
							{{ $t(`plugins.QualityAssurance.eventTypes.${event.type}`) }}
						</v-chip>
						<span v-if="event.subtype" class="text-caption ml-1">{{ event.subtype }}</span>
						<span v-if="event.device !== null" class="text-caption text-medium-emphasis ml-1">#{{ event.device }}</span>
					</td>
					<td class="text-caption">{{ eventDetail(event) }}</td>
					<td class="text-caption text-no-wrap">{{ position(event) }}</td>
				</tr>
				<tr v-if="visible.length === 0">
					<td colspan="5" class="text-center text-medium-emphasis">{{ $t("plugins.QualityAssurance.events.none") }}</td>
				</tr>
			</tbody>
		</v-table>
	</div>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

import type { QaEvent } from "../core/api";
import { eventColor, eventDetail, formatClock } from "../core/format";

export default defineComponent({
	props: {
		events: { type: Array as PropType<Array<QaEvent>>, default: () => [] },
		/** epoch ms of the job start: times are shown as elapsed time */
		startMs: { type: Number, default: 0 }
	},
	emits: ["select"],
	data() {
		return { hidden: [] as Array<string> };
	},
	computed: {
		counts(): Record<string, number> {
			const counts: Record<string, number> = {};
			for (const event of this.events) {
				counts[event.type] = (counts[event.type] ?? 0) + 1;
			}
			return counts;
		},
		types(): Array<string> {
			return Object.keys(this.counts).sort();
		},
		visible(): Array<QaEvent> {
			return this.events.filter((e) => !this.hidden.includes(e.type));
		}
	},
	methods: {
		eventColor,
		eventDetail,
		clock(ts: number): string {
			return formatClock((ts - this.startMs) / 1000);
		},
		toggle(type: string) {
			this.hidden = this.hidden.includes(type) ? this.hidden.filter((t) => t !== type) : [...this.hidden, type];
		},
		position(event: QaEvent): string {
			if (event.x === null && event.y === null) {
				return "—";
			}
			const f = (v: number | null) => (v === null ? "—" : v.toFixed(1));
			return `X${f(event.x)} Y${f(event.y)} Z${f(event.z)}`;
		}
	}
});
</script>

<style scoped>
.qa-event {
	cursor: pointer;
}
</style>
