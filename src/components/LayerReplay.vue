<template>
	<div class="qa-layer-replay">
		<replay-canvas :toolpath="toolpath" :executed="executed" :markers="markers" :height="0" />
		<div v-if="message" class="qa-layer-replay__message text-body-2 text-medium-emphasis">{{ message }}</div>
	</div>
</template>

<script lang="ts">
import { defineComponent, markRaw, type PropType } from "vue";

import type { JobDetail, LayersAnswer, QaApi, QaEvent, SamplesAnswer, ToolpathAnswer } from "../core/api";
import { pluginData } from "../core/backend";
import {
	executedPoints, fetchToolpath, frameAt, LAYER_SETTLE_MS, layerEvents, layerSpan, machineToUser, markerColor, nozzleHeaters,
	replayChannels, SAMPLE_LEAD_MS, toolOffsets, TOOLPATH_RETRY_MS
} from "../core/replay";
import { createHost, sharedApi } from "../host";
import ReplayCanvas, { type ReplayMarker } from "./ReplayCanvas.vue";

/**
 * One layer's replay for another plugin's page (registered as the embeddable
 * "QualityAssurance.LayerReplay"; the CHX 350 analysis shows it as its top view): the toolpath
 * coloured by commanded flow, the executed moves' planned extrusion rate as dots and the layer's events as
 * rings, filling the height the page gives it. The page chooses job and layer; the component
 * talks to the daemon itself. docs/chx-integration.md §4
 */
export default defineComponent({
	components: { ReplayCanvas },
	props: {
		/** QA job id; empty follows the job QA records now, else the last one */
		jobId: { type: String, default: "" },
		/** Layer in job.layer numbering; 0 shows the job's last recorded layer */
		layer: { type: Number, default: 0 },
		/** Tests pass a fake; embedded, the component uses the shared client on DWC's store */
		api: { type: Object as PropType<QaApi | null>, default: null }
	},
	data() {
		return {
			job: null as JobDetail | null,
			layers: null as LayersAnswer | null,
			events: [] as Array<QaEvent>,
			toolpath: null as ToolpathAnswer | null,
			samples: null as SamplesAnswer | null,
			message: null as string | null,
			/** Retry after a 202, or the settle delay of a layer change */
			timer: null as ReturnType<typeof setTimeout> | null,
			/** Bumped per job and per layer request; answers of a superseded one are dropped */
			jobRequest: 0,
			request: 0
		};
	},
	computed: {
		client(): QaApi {
			return this.api ?? sharedApi();
		},
		resolvedJobId(): string {
			if (this.jobId) {
				return this.jobId;
			}
			const entry = createHost().pluginEntry();
			return String(pluginData(entry, "currentJobId") || pluginData(entry, "lastJobId") || "");
		},
		resolvedLayer(): number {
			if (this.layer > 0) {
				return this.layer;
			}
			const recorded = this.layers?.layers ?? [];
			return recorded.length ? recorded[recorded.length - 1].layer : 1;
		},
		span(): { from: number; to: number } | null {
			return layerSpan(this.layers, this.resolvedLayer);
		},
		area(): number {
			const d = this.toolpath?.meta.filamentDiameter ?? 1.75;
			return Math.PI * (d / 2) ** 2;
		},
		executed(): Array<{ x: number; y: number; flow: number }> {
			return executedPoints(this.samples?.channels ?? {}, this.area, this.span?.from)
				.map((p) => ({ ...this.userPoint(p.x, p.y, p.ts), flow: p.flow }));
		},
		markers(): Array<ReplayMarker> {
			return layerEvents(this.events, this.resolvedLayer).filter((e) => e.x !== null && e.y !== null).map((e) => ({
				...this.userPoint(e.x as number, e.y as number, e.ts_ms),
				color: markerColor(e.type),
				label: e.type
			}));
		}
	},
	watch: {
		resolvedJobId() {
			this.init();
		},
		resolvedLayer() {
			this.stopTimer();
			// following the job, the last layer resolves with the job's data: load it at once
			if (this.layer <= 0) {
				this.load();
				return;
			}
			this.timer = setTimeout(() => this.load(), LAYER_SETTLE_MS);
		}
	},
	mounted() {
		this.init();
	},
	beforeUnmount() {
		this.jobRequest++;
		this.request++;
		this.stopTimer();
	},
	methods: {
		/** Machine X/Y at ``ts`` in G-code coordinates, with the workplace and tool of that time */
		userPoint(x: number, y: number, ts: number): { x: number; y: number } {
			const frame = frameAt(this.events, ts);
			return machineToUser({ x, y }, frame.offsets, toolOffsets(this.job?.context, frame.tool));
		},
		stopTimer() {
			if (this.timer !== null) {
				clearTimeout(this.timer);
				this.timer = null;
			}
		},
		/** The job's context, layer times and events, once per job; then the layer */
		async init() {
			const jobRequest = ++this.jobRequest;
			this.job = null;
			this.layers = null;
			this.events = [];
			this.toolpath = null;
			this.samples = null;
			this.message = null;
			const id = this.resolvedJobId;
			if (!id) {
				this.message = this.$t("plugins.QualityAssurance.embed.noJob");
				return;
			}
			this.load();
			try {
				const [job, layers, events] = await Promise.all([this.client.job(id), this.client.layers(id), this.client.events(id)]);
				if (jobRequest !== this.jobRequest) {
					return;
				}
				const layerBefore = this.resolvedLayer;
				this.job = job;
				this.layers = markRaw(layers);
				this.events = markRaw(events.events);
				// The layer times are known now, so the executed points can follow. Without a layer
				// prop the last layer resolves only now; the watcher loads that one
				if (this.resolvedLayer === layerBefore) {
					await this.loadSamples(this.request, id);
				}
			} catch (e) {
				if (jobRequest === this.jobRequest) {
					this.message = e instanceof Error ? e.message : String(e);
				}
			}
		},
		async load() {
			const request = ++this.request;
			this.stopTimer();
			const id = this.resolvedJobId;
			if (!id) {
				return;
			}
			const result = await fetchToolpath(this.client, id, this.resolvedLayer);
			if (request !== this.request) {
				return;
			}
			if (result.state === "building") {
				this.message = this.$t("plugins.QualityAssurance.replay.building");
				this.timer = setTimeout(() => this.load(), TOOLPATH_RETRY_MS);
				return;
			}
			if (result.state === "ready") {
				this.message = null;
				this.toolpath = markRaw(result.toolpath);
			} else {
				this.toolpath = null;
				this.message = result.state === "error" ? result.message : this.$t(`plugins.QualityAssurance.replay.${result.state}`);
			}
			await this.loadSamples(request, id);
		},
		async loadSamples(request: number, id: string) {
			const span = this.span;
			if (!span || this.job?.rawPruned) {
				this.samples = null;
				return;
			}
			try {
				const samples = await this.client.samples(id, replayChannels(nozzleHeaters(this.layers)),
					{ from: span.from - SAMPLE_LEAD_MS, to: span.to, resolution: "auto" });
				if (request === this.request) {
					this.samples = markRaw(samples);
				}
			} catch {
				if (request === this.request) {
					this.samples = null;  // the toolpath stands without them
				}
			}
		}
	}
});
</script>

<style scoped>
.qa-layer-replay {
	position: relative;
	height: 100%;
	min-height: 160px;
}
.qa-layer-replay__message {
	position: absolute;
	inset: 0;
	display: flex;
	align-items: center;
	justify-content: center;
	padding: 16px;
	text-align: center;
	pointer-events: none;
}
</style>
