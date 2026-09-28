<template>
	<div class="qa-layer-timelapse">
		<timelapse-frame v-if="hasFrames" :api="client" :job-id="resolvedJobId" :meta="meta" :layer="frameLayer" fill />
		<div v-else class="qa-layer-timelapse__message text-body-2 text-medium-emphasis">{{ message }}</div>
	</div>
</template>

<script lang="ts">
import { defineComponent, markRaw, type PropType } from "vue";

import type { QaApi, TimelapseMeta } from "../core/api";
import { pluginData } from "../core/backend";
import { LAYER_SETTLE_MS } from "../core/replay";
import { isPending, layersWithFrames, timelapseNote } from "../core/timelapse";
import { createHost, sharedApi } from "../host";
import TimelapseFrame from "./TimelapseFrame.vue";

const POLL_MS = 5000;

/**
 * The camera frame of one layer for another plugin's page (registered as the embeddable
 * "QualityAssurance.LayerTimelapse"; the CHX 350 analysis shows it as its timelapse view),
 * filling the height the page gives it. Asks again while the timelapse is still recorded or
 * encoded, so a running job gains its frames. docs/chx-integration.md §4
 */
export default defineComponent({
	components: { TimelapseFrame },
	props: {
		/** QA job id; empty follows the job QA records now, else the last one */
		jobId: { type: String, default: "" },
		/** Layer in job.layer numbering; 0 shows the latest layer with a frame */
		layer: { type: Number, default: 0 },
		/** Tests pass a fake; embedded, the component uses the shared client on DWC's store */
		api: { type: Object as PropType<QaApi | null>, default: null }
	},
	data() {
		return {
			meta: null as TimelapseMeta | null,
			error: null as string | null,
			timer: null as ReturnType<typeof setTimeout> | null,
			/** The layer whose frame shows: follows the layer once it settles (each frame is a request) */
			frameLayer: 0,
			settle: null as ReturnType<typeof setTimeout> | null,
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
		framedLayers(): Array<number> {
			return layersWithFrames(this.meta);
		},
		hasFrames(): boolean {
			return this.framedLayers.length > 0;
		},
		resolvedLayer(): number {
			return this.layer > 0 ? this.layer : (this.framedLayers[this.framedLayers.length - 1] ?? 1);
		},
		message(): string {
			if (!this.resolvedJobId) {
				return this.$t("plugins.QualityAssurance.embed.noJob");
			}
			if (this.error) {
				return this.error;
			}
			if (!this.meta) {
				return this.$t("plugins.QualityAssurance.timelapse.loading");
			}
			const note = timelapseNote(this.meta);
			return note ? this.$t(`plugins.QualityAssurance.timelapse.${note.key}`, note.params ?? {}) : this.$t("plugins.QualityAssurance.timelapse.noFrame");
		}
	},
	watch: {
		resolvedLayer: {
			handler(layer: number) {
				if (this.settle !== null) {
					clearTimeout(this.settle);
				}
				// the first layer, and the latest frame while following the job, show at once
				if (this.frameLayer === 0 || this.layer <= 0) {
					this.frameLayer = layer;
					return;
				}
				this.settle = setTimeout(() => {
					this.settle = null;
					this.frameLayer = layer;
				}, LAYER_SETTLE_MS);
			},
			immediate: true
		},
		resolvedJobId() {
			this.meta = null;
			this.load();
		}
	},
	mounted() {
		this.load();
	},
	beforeUnmount() {
		this.request++;
		this.stopTimer();
		if (this.settle !== null) {
			clearTimeout(this.settle);
		}
	},
	methods: {
		stopTimer() {
			if (this.timer !== null) {
				clearTimeout(this.timer);
				this.timer = null;
			}
		},
		async load() {
			const request = ++this.request;
			this.stopTimer();
			const id = this.resolvedJobId;
			if (!id) {
				return;
			}
			try {
				const meta = await this.client.timelapseMeta(id);
				if (request !== this.request) {
					return;
				}
				this.meta = markRaw(meta);
				this.error = null;
			} catch (e) {
				if (request === this.request) {
					this.error = e instanceof Error ? e.message : String(e);
				}
			}
			if (request === this.request && isPending(this.meta)) {
				this.timer = setTimeout(() => this.load(), POLL_MS);
			}
		}
	}
});
</script>

<style scoped>
.qa-layer-timelapse {
	position: relative;
	height: 100%;
	min-height: 160px;
}
.qa-layer-timelapse__message {
	height: 100%;
	display: flex;
	align-items: center;
	justify-content: center;
	padding: 16px;
	text-align: center;
}
</style>
