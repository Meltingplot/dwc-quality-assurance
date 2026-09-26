<template>
	<v-table density="compact" class="qa-context">
		<tbody>
			<tr v-for="row in rows" :key="row.key">
				<td class="text-no-wrap text-medium-emphasis" :style="{ paddingLeft: `${16 + row.depth * 16}px` }">{{ row.label }}</td>
				<td class="qa-context__value">{{ row.value }}</td>
			</tr>
		</tbody>
	</v-table>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

interface Row {
	key: string;
	label: string;
	value: string;
	depth: number;
}

function flatten(value: unknown, label: string, key: string, depth: number, rows: Array<Row>) {
	if (value !== null && typeof value === "object" && !Array.isArray(value)) {
		rows.push({ key, label, value: "", depth });
		for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
			flatten(v, k, `${key}.${k}`, depth + 1, rows);
		}
	} else if (Array.isArray(value) && value.some((v) => v !== null && typeof v === "object")) {
		rows.push({ key, label, value: "", depth });
		value.forEach((v, i) => flatten(v, `[${i}]`, `${key}[${i}]`, depth + 1, rows));
	} else {
		rows.push({ key, label, value: value === null || value === undefined ? "—" : Array.isArray(value) ? value.join(", ") : String(value), depth });
	}
}

/** The job context (and summary) as an indented key/value table */
export default defineComponent({
	props: {
		data: { type: Object as PropType<Record<string, unknown> | null>, default: null }
	},
	computed: {
		rows(): Array<Row> {
			const rows: Array<Row> = [];
			for (const [k, v] of Object.entries(this.data ?? {})) {
				flatten(v, k, k, 0, rows);
			}
			return rows;
		}
	}
});
</script>

<style scoped>
.qa-context__value {
	font-family: monospace;
	word-break: break-all;
}
</style>
