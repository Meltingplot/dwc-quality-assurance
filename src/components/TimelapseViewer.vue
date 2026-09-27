<template>
	<div>
		<v-alert v-if="error" type="error" variant="tonal" density="compact" class="mb-2">{{ error }}</v-alert>
		<v-progress-linear v-if="!meta && !error" indeterminate color="primary" class="mb-2" />
		<template v-if="meta">
			<div class="d-flex flex-wrap align-center ga-2 mb-2">
				<v-chip :color="statusColor" variant="tonal">{{ $t(`plugins.QualityAssurance.timelapse.states.${meta.status}`) }}</v-chip>
				<span v-if="meta.frames" class="text-body-2 text-medium-emphasis">
					{{ $t("plugins.QualityAssurance.timelapse.detail", { frames: meta.frames, fps: meta.fps ?? "—" }) }}
				</span>
				<v-spacer />
				<v-btn v-if="videoSrc" :href="videoSrc" :download="`qa-${job.id}-timelapse.mp4`" variant="text" size="small"
					prepend-icon="mdi-download">
					{{ $t("plugins.QualityAssurance.timelapse.download") }} ({{ formatBytes(meta.sizeBytes) }})
				</v-btn>
			</div>
			<v-alert v-if="note" :type="meta.status === 'failed' ? 'warning' : 'info'" variant="tonal" density="compact" class="mb-2">
				{{ note }}
			</v-alert>
			<template v-if="layers.length">
				<template v-if="meta.video">
					<video v-if="videoSrc" ref="video" :src="videoSrc" controls playsinline preload="auto" class="qa-video"
						@timeupdate="onTime" />
					<div v-else class="text-caption text-medium-emphasis">{{ $t("plugins.QualityAssurance.timelapse.loading") }}</div>
				</template>
				<timelapse-frame v-else :api="api" :job-id="job.id" :meta="meta" :layer="layer" />
				<div class="d-flex align-center ga-2 mt-2">
					<v-slider v-model="index" :min="0" :max="layers.length - 1" :step="1" hide-details density="compact"
						class="qa-slider" @update:model-value="seekTo" />
					<span class="text-body-2 text-no-wrap">
						{{ $t("plugins.QualityAssurance.replay.layerOf", { layer, total: layers[layers.length - 1] }) }}
					</span>
				</div>
			</template>
		</template>
	</div>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

import type { JobDetail, QaApi, TimelapseMeta } from "../core/api";
import { formatBytes } from "../core/format";
import { frameForLayer, isPending, layerAtTime, layersWithFrames, seekTime, videoUrl } from "../core/timelapse";
import TimelapseFrame from "./TimelapseFrame.vue";

const POLL_MS = 5000;
const STATUS_COLORS: Record<string, string> = { done: "success", failed: "warning", capturing: "primary", queued: "info",
	encoding: "info", pruned: "grey", none: "grey" };

/**
 * A job's timelapse (PLAN.md §5.11): the AV1 video once it is encoded (downloaded once, then
 * seeked locally: DSF serves files without Range support), a layer slider that follows it, the
 * download; before the video or after a failed encoding the captured frames per layer.
 */
export default defineComponent({
	components: { TimelapseFrame },
	props: {
		api: { type: Object as PropType<QaApi>, required: true },
		job: { type: Object as PropType<JobDetail>, required: true }
	},
	data() {
		return {
			meta: null as TimelapseMeta | null,
			error: null as string | null,
			videoSrc: null as string | null,
			index: 0,
			timer: null as ReturnType<typeof setTimeout> | null,
			request: 0
		};
	},
	computed: {
		layers(): Array<number> {
			return layersWithFrames(this.meta);
		},
		layer(): number {
			return this.layers[Math.min(this.index, this.layers.length - 1)] ?? 1;
		},
		statusColor(): string {
			return STATUS_COLORS[this.meta?.status ?? "none"] ?? "grey";
		},
		note(): string | null {
			const meta = this.meta;
			switch (meta?.status) {
				case "none":
					return this.$t("plugins.QualityAssurance.timelapse.none", { reason: meta.reason ?? "—" });
				case "capturing":
					return this.$t("plugins.QualityAssurance.timelapse.capturing");
				case "queued":
				case "encoding":
					return this.$t("plugins.QualityAssurance.timelapse.encoding");
				case "failed":
					return this.$t("plugins.QualityAssurance.timelapse.failed", { error: meta.error ?? "—" });
				case "pruned":
					return this.$t("plugins.QualityAssurance.timelapse.pruned");
				default:
					return null;
			}
		}
	},
	watch: {
		"job.id"() {
			this.videoSrc = null;
			this.meta = null;
			this.load();
		}
	},
	mounted() {
		this.load();
	},
	beforeUnmount() {
		this.request++;
		if (this.timer !== null) {
			clearTimeout(this.timer);
		}
	},
	methods: {
		formatBytes,
		async load() {
			const request = ++this.request;
			if (this.timer !== null) {
				clearTimeout(this.timer);
				this.timer = null;
			}
			try {
				const meta = await this.api.timelapseMeta(this.job.id);
				if (request !== this.request) {
					return;
				}
				this.meta = meta;
				this.error = null;
				if (this.index >= this.layers.length) {
					this.index = Math.max(0, this.layers.length - 1);
				}
				if (meta.video && !this.videoSrc) {
					this.videoSrc = await videoUrl(this.api, this.job.id);
				}
			} catch (e) {
				if (request === this.request) {
					this.error = e instanceof Error ? e.message : String(e);
				}
			}
			if (request === this.request && isPending(this.meta)) {
				this.timer = setTimeout(() => this.load(), POLL_MS);
			}
		},
		seekTo() {
			const video = this.$refs.video as HTMLVideoElement | undefined;
			const shown = frameForLayer(this.meta, this.layer);
			if (!video || !shown || !this.meta?.fps) {
				return;
			}
			try {
				video.currentTime = seekTime(shown.frame, this.meta.fps);
			} catch {
				// not seekable yet
			}
		},
		onTime() {
			const video = this.$refs.video as HTMLVideoElement | undefined;
			const layer = video ? layerAtTime(this.meta, video.currentTime) : null;
			const index = layer === null ? -1 : this.layers.indexOf(layer);
			if (index >= 0) {
				this.index = index;
			}
		}
	}
});
</script>

<style scoped>
.qa-video {
	display: block;
	width: 100%;
	max-height: 70vh;
	border-radius: 4px;
	background: #000;
}
.qa-slider {
	flex: 1 1 300px;
}
</style>
