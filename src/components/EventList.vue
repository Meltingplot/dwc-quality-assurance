<template>
	<div>
		<div class="d-flex flex-wrap ga-1 mb-1">
			<v-chip v-for="level in levels" :key="level" size="small" :color="levelColor(level)" class="qa-level"
				:variant="hiddenLevels.includes(level) ? 'outlined' : 'flat'" @click="toggleLevel(level)">
				{{ $t(`plugins.QualityAssurance.levels.${level}`) }} ({{ levelCounts[level] }})
			</v-chip>
			<span class="text-caption text-medium-emphasis align-self-center ml-1">{{ $t("plugins.QualityAssurance.events.countedHint") }}</span>
		</div>
		<div class="d-flex flex-wrap ga-1 mb-2">
			<v-chip v-for="type in types" :key="type" size="small" :color="typeColor(type)" class="qa-type"
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
						<v-chip size="x-small" :color="eventColor(event)" variant="tonal"
							:title="$t(`plugins.QualityAssurance.levels.${event.severity}`)">
							{{ $t(`plugins.QualityAssurance.eventTypes.${event.type}`) }}
						</v-chip>
						<span v-if="event.subtype" class="text-caption ml-1">{{ event.subtype }}</span>
						<span v-if="event.device !== null" class="text-caption text-medium-emphasis ml-1">#{{ event.device }}</span>
						<v-chip v-if="event.payload?.confirmed === false" size="x-small" variant="outlined" class="ml-1"
							:title="$t('plugins.QualityAssurance.events.unconfirmedHint')">
							{{ $t("plugins.QualityAssurance.events.unconfirmed") }}
						</v-chip>
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
import { EVENT_LEVELS, type EventLevel, eventColor, eventDetail, formatClock, levelColor, worstLevel } from "../core/format";

export default defineComponent({
	props: {
		events: { type: Array as PropType<Array<QaEvent>>, default: () => [] },
		/** epoch ms of the job start: times are shown as elapsed time */
		startMs: { type: Number, default: 0 }
	},
	emits: ["select"],
	data() {
		return { hidden: [] as Array<string>, hiddenLevels: [] as Array<EventLevel>, levels: EVENT_LEVELS };
	},
	computed: {
		levelCounts(): Record<EventLevel, number> {
			const counts = { error: 0, warning: 0, info: 0 };
			for (const event of this.events) {
				counts[event.severity] = (counts[event.severity] ?? 0) + 1;
			}
			return counts;
		},
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
			return this.events.filter((e) => !this.hidden.includes(e.type) && !this.hiddenLevels.includes(e.severity));
		}
	},
	methods: {
		eventColor,
		eventDetail,
		levelColor,
		/** A type's chip: the highest level among its events */
		typeColor(type: string): string {
			return levelColor(worstLevel(this.events.filter((e) => e.type === type)));
		},
		toggleLevel(level: EventLevel) {
			this.hiddenLevels = this.hiddenLevels.includes(level) ? this.hiddenLevels.filter((l) => l !== level) : [...this.hiddenLevels, level];
		},
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
