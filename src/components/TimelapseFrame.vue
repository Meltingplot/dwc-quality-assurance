<template>
	<div class="qa-frame">
		<video v-if="videoSrc" ref="video" :src="videoSrc" muted playsinline preload="auto" class="qa-frame__media"
			@loadedmetadata="seek" />
		<img v-else-if="imageSrc" :src="imageSrc" class="qa-frame__media" :alt="caption">
		<div v-else class="qa-frame__empty text-caption text-medium-emphasis">{{ message }}</div>
		<div v-if="shown" class="text-caption text-medium-emphasis mt-1">{{ caption }}</div>
	</div>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

import type { QaApi, TimelapseMeta } from "../core/api";
import { frameForLayer, seekTime, videoUrl } from "../core/timelapse";

/**
 * The timelapse frame of one layer: a paused `<video>` seeked to the frame once the video
 * exists (loaded once per job, PLAN.md §5.11), before that the captured JPEG from
 * `job/timelapse/frame`. A layer without its own frame shows the latest earlier one.
 */
export default defineComponent({
	props: {
		api: { type: Object as PropType<QaApi>, required: true },
		jobId: { type: String, required: true },
		meta: { type: Object as PropType<TimelapseMeta | null>, default: null },
		layer: { type: Number, required: true }
	},
	data() {
		return {
			videoSrc: null as string | null,
			imageSrc: null as string | null,
			loading: false,
			failed: null as string | null,
			request: 0
		};
	},
	computed: {
		shown(): { layer: number; frame: number } | null {
			return frameForLayer(this.meta, this.layer);
		},
		caption(): string {
			if (!this.shown) {
				return "";
			}
			return this.shown.layer === this.layer ? this.$t("plugins.QualityAssurance.timelapse.frameOf", { layer: this.layer })
				: this.$t("plugins.QualityAssurance.timelapse.frameOfEarlier", { layer: this.shown.layer });
		},
		message(): string {
			if (this.loading) {
				return this.$t("plugins.QualityAssurance.timelapse.loading");
			}
			return this.failed ?? this.$t("plugins.QualityAssurance.timelapse.noFrame");
		}
	},
	watch: {
		"meta.video"() {
			this.load();
		},
		jobId() {
			this.load();
		},
		shown(next: { frame: number } | null, previous: { frame: number } | null) {
			if (next?.frame !== previous?.frame) {
				this.update();
			}
		}
	},
	mounted() {
		this.load();
	},
	beforeUnmount() {
		this.request++;
		this.releaseImage();
	},
	methods: {
		async load() {
			const request = ++this.request;
			this.failed = null;
			if (this.meta?.video) {
				this.loading = true;
				try {
					const url = await videoUrl(this.api, this.jobId);
					if (request === this.request) {
						this.releaseImage();
						this.videoSrc = url;
					}
				} catch (e) {
					if (request === this.request) {
						this.failed = e instanceof Error ? e.message : String(e);
					}
				} finally {
					if (request === this.request) {
						this.loading = false;
					}
				}
				return;
			}
			this.videoSrc = null;
			await this.update();
		},
		seek() {
			const video = this.$refs.video as HTMLVideoElement | undefined;
			if (!video || !this.shown || !this.meta?.fps) {
				return;
			}
			try {
				video.currentTime = seekTime(this.shown.frame, this.meta.fps);
			} catch {
				// not seekable yet: loadedmetadata seeks again
			}
		},
		async update() {
			if (this.videoSrc) {
				this.seek();
				return;
			}
			const shown = this.shown;
			if (!shown) {
				this.releaseImage();
				return;
			}
			const request = ++this.request;
			this.loading = this.imageSrc === null;
			try {
				const blob = await this.api.timelapseFrame(this.jobId, shown.layer);
				if (request !== this.request) {
					return;
				}
				this.releaseImage();
				this.imageSrc = URL.createObjectURL(new Blob([blob], { type: "image/jpeg" }));
				this.failed = null;
			} catch (e) {
				if (request === this.request) {
					this.releaseImage();
					this.failed = e instanceof Error ? e.message : String(e);
				}
			} finally {
				if (request === this.request) {
					this.loading = false;
				}
			}
		},
		releaseImage() {
			if (this.imageSrc) {
				URL.revokeObjectURL(this.imageSrc);
				this.imageSrc = null;
			}
		}
	}
});
</script>

<style scoped>
.qa-frame__media {
	display: block;
	width: 100%;
	height: auto;
	border-radius: 4px;
	background: rgba(128, 128, 128, 0.1);
}
.qa-frame__empty {
	display: flex;
	align-items: center;
	justify-content: center;
	aspect-ratio: 16 / 9;
	border-radius: 4px;
	background: rgba(128, 128, 128, 0.06);
}
</style>
