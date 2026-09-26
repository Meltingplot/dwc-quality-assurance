<template>
	<div>
		<div class="d-flex flex-wrap align-center ga-2 mb-2">
			<v-btn icon="mdi-skip-previous" size="small" variant="text" :disabled="layer <= 1" @click="step(-1)" />
			<v-btn :icon="playing ? 'mdi-pause' : 'mdi-play'" size="small" variant="text" @click="togglePlay" />
			<v-btn icon="mdi-skip-next" size="small" variant="text" :disabled="layer >= maxLayer" @click="step(1)" />
			<v-slider v-model="layer" :min="1" :max="Math.max(1, maxLayer)" :step="1" hide-details thumb-label density="compact"
				class="qa-slider" />
			<span class="text-body-2 text-no-wrap">{{ $t("plugins.QualityAssurance.replay.layerOf", { layer, total: maxLayer }) }}</span>
			<v-select v-model="speed" :items="speeds" density="compact" variant="outlined" hide-details class="qa-speed"
				:label="$t('plugins.QualityAssurance.replay.speed')" />
			<v-switch v-model="showTravel" color="primary" density="compact" hide-details :label="$t('plugins.QualityAssurance.replay.travel')" />
		</div>
		<v-alert v-if="message" :type="messageType" variant="tonal" density="compact" class="mb-2">{{ message }}</v-alert>
		<v-row density="compact">
			<v-col cols="12" md="8">
				<replay-canvas :toolpath="toolpath" :measured="measured" :markers="markers" :hidden-objects="hiddenObjects"
					:show-travel="showTravel" />
				<div class="text-caption text-medium-emphasis mt-1">{{ $t("plugins.QualityAssurance.replay.legend") }}</div>
			</v-col>
			<v-col cols="12" md="4">
				<div v-if="objectList.length" class="mb-3">
					<div class="text-subtitle-2">{{ $t("plugins.QualityAssurance.replay.objects") }}</div>
					<v-checkbox v-for="obj in objectList" :key="obj.id" :model-value="!hiddenObjects.includes(obj.id)" :label="obj.name"
						density="compact" hide-details @update:model-value="toggleObject(obj.id, $event)" />
				</div>
				<div class="text-subtitle-2">{{ $t("plugins.QualityAssurance.replay.events") }}</div>
				<div v-if="layerEvents.length === 0" class="text-caption text-medium-emphasis mb-2">{{ $t("plugins.QualityAssurance.events.none") }}</div>
				<div v-for="event in layerEvents" :key="event.id" class="text-caption">
					<v-chip size="x-small" :color="eventColor(event.type)" variant="tonal">{{ $t(`plugins.QualityAssurance.eventTypes.${event.type}`) }}</v-chip>
					{{ event.subtype }} {{ eventDetail(event) }}
				</div>
				<div class="text-subtitle-2 mt-3">{{ $t("plugins.QualityAssurance.replay.curves") }}</div>
				<chart-canvas :config="curvesConfig" :height="200" />
			</v-col>
		</v-row>
	</div>
</template>

<script lang="ts">
import { defineComponent, markRaw, type PropType } from "vue";

import { statusOf, type JobDetail, type LayersAnswer, type QaApi, type QaEvent, type SamplesAnswer, type ToolpathAnswer } from "../core/api";
import { timeSeriesConfig } from "../core/charts";
import { eventColor, eventDetail } from "../core/format";
import { frameAt, machineToUser, measuredPoints, toolOffsets } from "../core/replay";
import ChartCanvas from "./ChartCanvas.vue";
import ReplayCanvas, { type ReplayMarker } from "./ReplayCanvas.vue";

const MARKER_COLORS: Record<string, string> = { error: "#E53935", warning: "#FB8C00", info: "#1E88E5", primary: "#1976D2", success: "#43A047", grey: "#9E9E9E" };
const RETRY_MS = 2000;
/** Samples start this much before the layer: fine rows hold only changed values, the coarse row
 * before the layer has the positions (two intervals at the default sampleIntervalS of 5 s) */
const SAMPLE_LEAD_MS = 10000;

/** Layer by layer: toolpath from the G-code (parsed on demand by the daemon), measured flow and
 * events on top, temperature and heater-load curves of the layer beside it (PLAN.md §3 Replay) */
export default defineComponent({
	components: { ChartCanvas, ReplayCanvas },
	props: {
		api: { type: Object as PropType<QaApi>, required: true },
		job: { type: Object as PropType<JobDetail>, required: true },
		layers: { type: Object as PropType<LayersAnswer | null>, default: null },
		events: { type: Array as PropType<Array<QaEvent>>, default: () => [] }
	},
	data() {
		return {
			layer: 1,
			maxLayer: 0,
			toolpath: null as ToolpathAnswer | null,
			samples: null as SamplesAnswer | null,
			message: null as string | null,
			messageType: "info" as "info" | "warning" | "error",
			playing: false,
			speed: 1,
			showTravel: false,
			hiddenObjects: [] as Array<number>,
			timer: null as ReturnType<typeof setTimeout> | null,
			retry: null as ReturnType<typeof setTimeout> | null,
			request: 0
		};
	},
	computed: {
		speeds(): Array<{ title: string; value: number }> {
			return [0.5, 1, 2, 5].map((value) => ({ title: `${value} / s`, value }));
		},
		nozzles(): Array<{ index: number; tool: number | null }> {
			return (this.layers?.meta.heaters ?? []).filter((h) => h.role === "nozzle");
		},
		layerRange(): { from: number; to: number } | null {
			const record = this.layers?.layers.find((l) => l.layer === this.layer);
			if (!record?.startedAt) {
				return null;
			}
			return { from: new Date(record.startedAt).getTime(), to: record.endedAt ? new Date(record.endedAt).getTime() : Date.now() };
		},
		area(): number {
			const d = this.toolpath?.meta.filamentDiameter ?? 1.75;
			return Math.PI * (d / 2) ** 2;
		},
		measured(): Array<{ x: number; y: number; flow: number }> {
			return measuredPoints(this.samples?.channels ?? {}, this.area, this.layerRange?.from)
				.map((p) => ({ ...this.userPoint(p.x, p.y, p.ts), flow: p.flow }));
		},
		layerEvents(): Array<QaEvent> {
			return this.events.filter((e) => e.layer === this.layer && !["job_start", "job_end"].includes(e.type));
		},
		markers(): Array<ReplayMarker> {
			return this.layerEvents.filter((e) => e.x !== null && e.y !== null && !(e.object_id !== null && this.hiddenObjects.includes(e.object_id))).map((e) => ({
				...this.userPoint(e.x as number, e.y as number, e.ts_ms),
				color: this.markerColor(e.type),
				label: e.type
			}));
		},
		objectList(): Array<{ id: number; name: string }> {
			return Object.entries(this.toolpath?.objects ?? {}).map(([id, name]) => ({ id: Number(id), name }));
		},
		curvesConfig(): any {
			const channels = this.samples?.channels ?? {};
			const from = this.layerRange?.from ?? 0;
			const clip = (points: Array<[number, number]> | undefined) => (points ?? []).filter(([ts]) => ts >= from);
			const series = this.nozzles.flatMap((h) => [
				{ label: `T${h.tool ?? h.index}`, unit: "°C", points: clip(channels[`heater.${h.index}.current`]) },
				{ label: `${this.$t("plugins.QualityAssurance.layers.loadMean")} T${h.tool ?? h.index}`, unit: "0..1",
					points: clip(channels[`heater.${h.index}.load`]), secondary: true }
			]);
			const markers = this.layerEvents.map((e) => ({ ts: e.ts_ms, label: e.type, color: this.markerColor(e.type) }));
			return timeSeriesConfig(series, from, { markers });
		}
	},
	watch: {
		layer() {
			this.load();
		},
		"job.id"() {
			this.init();
		}
	},
	mounted() {
		this.init();
	},
	beforeUnmount() {
		this.stopPlay();
		if (this.retry !== null) {
			clearTimeout(this.retry);
		}
	},
	methods: {
		eventColor,
		eventDetail,
		markerColor(type: string): string {
			return MARKER_COLORS[eventColor(type)] ?? MARKER_COLORS.grey;
		},
		init() {
			this.maxLayer = this.layers?.layers.length ? Math.max(...this.layers.layers.map((l) => l.layer)) : (this.job.numLayers ?? 0);
			this.layer = 1;
			this.load();
		},
		/** Machine X/Y at ``ts`` in G-code coordinates, with the workplace and tool of that time */
		userPoint(x: number, y: number, ts: number): { x: number; y: number } {
			const frame = frameAt(this.events, ts);
			return machineToUser({ x, y }, frame.offsets, toolOffsets(this.job.context, frame.tool));
		},
		async load() {
			const request = ++this.request;
			if (this.retry !== null) {
				clearTimeout(this.retry);
				this.retry = null;
			}
			try {
				const answer = await this.api.toolpath(this.job.id, this.layer);
				if (request !== this.request) {
					return;
				}
				if ("state" in answer) {
					this.message = this.$t("plugins.QualityAssurance.replay.building");
					this.messageType = "info";
					this.retry = setTimeout(() => this.load(), RETRY_MS);
					return;
				}
				this.message = null;
				this.toolpath = markRaw(answer);
				if (answer.meta.numLayers > this.maxLayer) {
					this.maxLayer = answer.meta.numLayers;
				}
			} catch (e) {
				if (request !== this.request) {
					return;
				}
				const status = statusOf(e);
				this.toolpath = null;
				this.messageType = status === 409 ? "warning" : "error";
				this.message = status === 409 ? this.$t("plugins.QualityAssurance.replay.fileGone")
					: status === 404 ? this.$t("plugins.QualityAssurance.replay.noLayer")
						: (e instanceof Error ? e.message : String(e));
			}
			this.loadSamples(request);
		},
		async loadSamples(request: number) {
			const range = this.layerRange;
			if (!range || this.job.rawPruned) {
				this.samples = null;
				return;
			}
			const channels = ["axis.X.machinePosition", "axis.Y.machinePosition", "move.currentMove.extrusionRate",
				...this.nozzles.flatMap((h) => [`heater.${h.index}.current`, `heater.${h.index}.load`])];
			try {
				const samples = await this.api.samples(this.job.id, channels, { from: range.from - SAMPLE_LEAD_MS, to: range.to, resolution: "auto" });
				if (request === this.request) {
					this.samples = markRaw(samples);
				}
			} catch {
				this.samples = null;
			}
		},
		step(delta: number) {
			this.layer = Math.max(1, Math.min(this.maxLayer, this.layer + delta));
		},
		togglePlay() {
			if (this.playing) {
				this.stopPlay();
			} else {
				this.playing = true;
				this.tick();
			}
		},
		tick() {
			this.timer = setTimeout(() => {
				if (!this.playing) {
					return;
				}
				if (this.layer >= this.maxLayer) {
					this.stopPlay();
					return;
				}
				this.step(1);
				this.tick();
			}, 1000 / this.speed);
		},
		stopPlay() {
			this.playing = false;
			if (this.timer !== null) {
				clearTimeout(this.timer);
				this.timer = null;
			}
		},
		toggleObject(id: number, visible: boolean | null) {
			this.hiddenObjects = visible ? this.hiddenObjects.filter((o) => o !== id) : [...this.hiddenObjects, id];
		}
	}
});
</script>

<style scoped>
.qa-slider {
	min-width: 200px;
	flex: 1 1 300px;
}
.qa-speed {
	max-width: 120px;
}
</style>
