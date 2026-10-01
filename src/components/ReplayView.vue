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
			<v-switch v-model="stacked" color="primary" density="compact" hide-details :label="$t('plugins.QualityAssurance.replay.stack')" />
		</div>
		<v-alert v-if="message" :type="messageType" variant="tonal" density="compact" class="mb-2">{{ message }}</v-alert>
		<div v-if="stacked && stackStatus" class="text-caption text-medium-emphasis mb-1">{{ stackStatus }}</div>
		<v-row density="compact">
			<v-col cols="12" md="8">
				<replay-canvas :toolpath="toolpath" :executed="executed" :markers="markers" :hidden-objects="hiddenObjects"
					:show-travel="showTravel" :stack="stacked ? stackLayers : null" :stack-bounds="stackMeta?.bounds ?? null" :layer="layer" />
				<div class="text-caption text-medium-emphasis mt-1">{{ legend }}</div>
			</v-col>
			<v-col cols="12" md="4">
				<timelapse-frame v-if="hasFrames" :api="api" :job-id="job.id" :meta="timelapse" :layer="layer" class="mb-3" />
				<div v-if="objectList.length" class="mb-3">
					<div class="text-subtitle-2">{{ $t("plugins.QualityAssurance.replay.objects") }}</div>
					<v-checkbox v-for="obj in objectList" :key="obj.id" :model-value="!hiddenObjects.includes(obj.id)" :label="obj.name"
						density="compact" hide-details @update:model-value="toggleObject(obj.id, $event)" />
				</div>
				<div class="text-subtitle-2">{{ $t("plugins.QualityAssurance.replay.events") }}</div>
				<div v-if="layerEvents.length === 0" class="text-caption text-medium-emphasis mb-2">{{ $t("plugins.QualityAssurance.events.none") }}</div>
				<div v-for="event in layerEvents" :key="event.id" class="text-caption">
					<v-chip size="x-small" :color="eventColor(event)" variant="tonal">{{ $t(`plugins.QualityAssurance.eventTypes.${event.type}`) }}</v-chip>
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

import type { JobDetail, LayersAnswer, QaApi, QaEvent, SamplesAnswer, TimelapseMeta, ToolpathAnswer, ToolpathStackAnswer } from "../core/api";
import { timeSeriesConfig } from "../core/charts";
import { eventColor, eventDetail } from "../core/format";
import {
	executedPoints, fetchToolpath, frameAt, layerEvents, layerSpan, machineToUser, markerColor, nozzleHeaters, replayChannels,
	SAMPLE_LEAD_MS, stackEvents, toolOffsets, TOOLPATH_RETRY_MS
} from "../core/replay";
import { fetchStack, toolpathZ, toStackLayer, zAtLayer, type StackLayer } from "../core/stack";
import { layersWithFrames } from "../core/timelapse";
import ChartCanvas from "./ChartCanvas.vue";
import ReplayCanvas, { type ReplayMarker } from "./ReplayCanvas.vue";
import TimelapseFrame from "./TimelapseFrame.vue";

/** Layer by layer: toolpath from the G-code (parsed on demand by the daemon), the executed moves' flow and
 * events on top, temperature and heater-load curves of the layer beside it (PLAN.md §3 Replay). The 3D
 * stack shows the layers below as well, reduced by the daemon and loaded piece by piece once it is
 * switched on, with the events of every layer up to the one shown */
export default defineComponent({
	components: { ChartCanvas, ReplayCanvas, TimelapseFrame },
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
			timelapse: null as TimelapseMeta | null,
			message: null as string | null,
			messageType: "info" as "info" | "warning" | "error",
			playing: false,
			speed: 1,
			showTravel: false,
			hiddenObjects: [] as Array<number>,
			timer: null as ReturnType<typeof setTimeout> | null,
			retry: null as ReturnType<typeof setTimeout> | null,
			request: 0,
			stacked: false,
			/** The stack as loaded so far, sorted by layer; a new array per answer */
			stackLayers: markRaw([]) as Array<StackLayer>,
			stackMeta: null as ToolpathStackAnswer["meta"] | null,
			/** Layer to load from next, null when the stack is complete */
			stackNext: 0 as number | null,
			stackError: null as string | null,
			/** The daemon answered 202: it indexes the file */
			stackBuilding: false,
			stackRetry: null as ReturnType<typeof setTimeout> | null,
			/** Bumped per job and when the stack is switched off; answers of a superseded one are dropped */
			stackRequest: 0
		};
	},
	computed: {
		speeds(): Array<{ title: string; value: number }> {
			return [0.5, 1, 2, 5].map((value) => ({ title: `${value} / s`, value }));
		},
		nozzles(): Array<{ index: number; tool: number | null }> {
			return nozzleHeaters(this.layers);
		},
		layerRange(): { from: number; to: number } | null {
			return layerSpan(this.layers, this.layer);
		},
		area(): number {
			const d = this.toolpath?.meta.filamentDiameter ?? 1.75;
			return Math.PI * (d / 2) ** 2;
		},
		executed(): Array<{ x: number; y: number; flow: number }> {
			return executedPoints(this.samples?.channels ?? {}, this.area, this.layerRange?.from)
				.map((p) => ({ ...this.userPoint(p.x, p.y, p.ts), flow: p.flow }));
		},
		layerEvents(): Array<QaEvent> {
			return layerEvents(this.events, this.layer);
		},
		/** Z of the layers known so far: the stack's and the one shown */
		knownZ(): Array<{ layer: number; z: number }> {
			const known = this.stackLayers.map((l) => ({ layer: l.layer, z: l.z }));
			const z = this.toolpath?.layer === this.layer ? toolpathZ(this.toolpath) : null;
			if (z !== null && !known.some((k) => k.layer === this.layer)) {
				known.push({ layer: this.layer, z });
				known.sort((a, b) => a.layer - b.layer);
			}
			return known;
		},
		markers(): Array<ReplayMarker> {
			const shown = (e: QaEvent) => e.x !== null && e.y !== null && !(e.object_id !== null && this.hiddenObjects.includes(e.object_id));
			const marker = (e: QaEvent) => ({ ...this.userPoint(e.x as number, e.y as number, e.ts_ms), color: markerColor(e), label: e.type });
			if (!this.stacked) {
				return this.layerEvents.filter(shown).map(marker);
			}
			// the layers below at their height in the stack (between two stack layers when it holds every n-th);
			// the layer's own on the layer drawn
			return stackEvents(this.events, this.layer).filter(shown).flatMap((e) => {
				if (e.layer === this.layer) {
					return [marker(e)];
				}
				const z = zAtLayer(this.knownZ, e.layer as number);
				return z === null ? [] : [{ ...marker(e), z, past: true }];
			});
		},
		legend(): string {
			const meta = this.stackMeta;
			return this.stacked && meta
				? this.$t("plugins.QualityAssurance.replay.stackLegend", { resolution: meta.resolution, step: meta.step })
				: this.$t("plugins.QualityAssurance.replay.legend");
		},
		stackStatus(): string | null {
			if (this.stackError) {
				return this.$t("plugins.QualityAssurance.replay.stackFailed", { message: this.stackError });
			}
			if (this.stackNext === null) {
				return null;
			}
			return this.stackBuilding
				? this.$t("plugins.QualityAssurance.replay.building")
				: this.$t("plugins.QualityAssurance.replay.stackLoading", { layer: this.stackNext || 1, total: this.stackMeta?.numLayers ?? this.maxLayer });
		},
		hasFrames(): boolean {
			return layersWithFrames(this.timelapse).length > 0;
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
			const markers = this.layerEvents.map((e) => ({ ts: e.ts_ms, label: e.type, color: markerColor(e) }));
			return timeSeriesConfig(series, from, { markers });
		}
	},
	watch: {
		layer() {
			this.load();
		},
		"job.id"() {
			this.init();
		},
		stacked(on: boolean) {
			if (on) {
				this.loadStack();
			} else {
				this.stopStack();
			}
		}
	},
	mounted() {
		this.init();
	},
	beforeUnmount() {
		this.stopPlay();
		this.stopStack();
		if (this.retry !== null) {
			clearTimeout(this.retry);
		}
	},
	methods: {
		eventColor,
		eventDetail,
		init() {
			this.maxLayer = this.layers?.layers.length ? Math.max(...this.layers.layers.map((l) => l.layer)) : (this.job.numLayers ?? 0);
			this.layer = 1;
			this.load();
			this.loadTimelapse();
			this.stopStack();
			this.stackLayers = markRaw([]);
			this.stackMeta = null;
			this.stackNext = 0;
			this.stackError = null;
			this.stackBuilding = false;
			if (this.stacked) {
				this.loadStack();
			}
		},
		/** The stack piece by piece from where it stopped; the answers come bottom up, so the canvas adds each on top */
		async loadStack() {
			this.stopStack();
			const request = this.stackRequest;
			const jobId = this.job.id;
			this.stackError = null;
			while (this.stackNext !== null) {
				const result = await fetchStack(this.api, jobId, this.stackNext);
				if (request !== this.stackRequest) {
					return;
				}
				this.stackBuilding = result.state === "building";
				if (result.state === "building") {
					this.stackRetry = setTimeout(() => this.loadStack(), TOOLPATH_RETRY_MS);
					return;
				}
				if (result.state !== "ready") {
					// a missing file already shows as the layer's message
					this.stackError = result.state === "error" ? result.message : null;
					this.stackNext = result.state === "fileGone" ? null : this.stackNext;
					return;
				}
				const layers = result.answer.layers.map(toStackLayer).filter((l): l is StackLayer => l !== null);
				this.stackLayers = markRaw([...this.stackLayers, ...layers]);
				this.stackMeta = markRaw(result.answer.meta);
				this.stackNext = result.answer.next;
			}
		},
		stopStack() {
			this.stackRequest++;
			if (this.stackRetry !== null) {
				clearTimeout(this.stackRetry);
				this.stackRetry = null;
			}
		},
		async loadTimelapse() {
			const jobId = this.job.id;
			try {
				const meta = await this.api.timelapseMeta(jobId);
				if (jobId === this.job.id) {
					this.timelapse = meta;
				}
			} catch {
				this.timelapse = null;  // the replay works without it
			}
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
			const result = await fetchToolpath(this.api, this.job.id, this.layer);
			if (request !== this.request) {
				return;
			}
			if (result.state === "building") {
				this.message = this.$t("plugins.QualityAssurance.replay.building");
				this.messageType = "info";
				this.retry = setTimeout(() => this.load(), TOOLPATH_RETRY_MS);
				return;
			}
			if (result.state === "ready") {
				this.message = null;
				this.toolpath = markRaw(result.toolpath);
				if (result.toolpath.meta.numLayers > this.maxLayer) {
					this.maxLayer = result.toolpath.meta.numLayers;
				}
			} else {
				this.toolpath = null;
				this.messageType = result.state === "fileGone" ? "warning" : "error";
				this.message = result.state === "error" ? result.message : this.$t(`plugins.QualityAssurance.replay.${result.state}`);
			}
			this.loadSamples(request);
		},
		async loadSamples(request: number) {
			const range = this.layerRange;
			if (!range || this.job.rawPruned) {
				this.samples = null;
				return;
			}
			try {
				const samples = await this.api.samples(this.job.id, replayChannels(this.nozzles), { from: range.from - SAMPLE_LEAD_MS, to: range.to, resolution: "auto" });
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
