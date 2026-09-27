<template>
	<div ref="box" class="qa-replay" @wheel.prevent="onWheel" @pointerdown="onDown" @pointermove="onMove" @pointerup="onUp"
		@pointerleave="onUp" @dblclick="fit">
		<canvas ref="canvas" />
		<div class="qa-replay__legend text-caption">
			<span>{{ formatNumber(range[0], 1) }}</span>
			<span class="qa-replay__ramp" :style="{ background: rampCss }" />
			<span>{{ formatNumber(range[1], 1) }} mm³/s</span>
		</div>
	</div>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

import type { ToolpathAnswer } from "../core/api";
import { formatNumber } from "../core/format";
import { fitView, flowRange, pan, rampColor, toolpathBounds, toScreen, zoomAt, type View } from "../core/replay";

export interface ReplayMarker {
	x: number;
	y: number;
	color: string;
	label: string;
}

/**
 * One layer's toolpath on a 2D canvas, coloured by commanded volumetric flow; measured samples
 * as dots coloured on the same scale, events as rings. Wheel zooms, drag pans, double click fits.
 */
export default defineComponent({
	props: {
		toolpath: { type: Object as PropType<ToolpathAnswer | null>, default: null },
		measured: { type: Array as PropType<Array<{ x: number; y: number; flow: number }>>, default: () => [] },
		markers: { type: Array as PropType<Array<ReplayMarker>>, default: () => [] },
		hiddenObjects: { type: Array as PropType<Array<number>>, default: () => [] },
		showTravel: { type: Boolean, default: false },
		height: { type: Number, default: 480 }
	},
	data() {
		return {
			view: null as View | null,
			drag: null as { x: number; y: number } | null,
			observer: null as ResizeObserver | null
		};
	},
	computed: {
		range(): [number, number] {
			return this.toolpath ? flowRange(this.toolpath) : [0, 1];
		},
		rampCss(): string {
			return `linear-gradient(90deg, ${[0, 0.25, 0.5, 0.75, 1].map(rampColor).join(", ")})`;
		}
	},
	watch: {
		toolpath(next: ToolpathAnswer | null, previous: ToolpathAnswer | null) {
			// keep the view while stepping through layers of the same job, fit on the first one
			if (!previous || !this.view) {
				this.fit();
			} else {
				this.draw();
			}
		},
		measured() { this.draw(); },
		markers() { this.draw(); },
		hiddenObjects() { this.draw(); },
		showTravel() { this.draw(); }
	},
	mounted() {
		if (typeof ResizeObserver !== "undefined") {
			this.observer = new ResizeObserver(() => this.draw());
			this.observer.observe(this.$refs.box as Element);
		}
		this.fit();
	},
	beforeUnmount() {
		this.observer?.disconnect();
	},
	methods: {
		formatNumber,
		size(): { width: number; height: number } {
			const box = this.$refs.box as HTMLElement | undefined;
			return { width: Math.max(100, box?.clientWidth || 600), height: this.height };
		},
		fit() {
			const bounds = this.toolpath ? toolpathBounds(this.toolpath, false) ?? toolpathBounds(this.toolpath) : null;
			const { width, height } = this.size();
			this.view = bounds ? fitView(bounds, width, height) : null;
			this.draw();
		},
		onWheel(event: WheelEvent) {
			if (!this.view) {
				return;
			}
			this.view = zoomAt(this.view, event.deltaY < 0 ? 1.2 : 1 / 1.2, event.offsetX, event.offsetY);
			this.draw();
		},
		onDown(event: PointerEvent) {
			this.drag = { x: event.clientX, y: event.clientY };
		},
		onMove(event: PointerEvent) {
			if (!this.drag || !this.view) {
				return;
			}
			this.view = pan(this.view, event.clientX - this.drag.x, event.clientY - this.drag.y);
			this.drag = { x: event.clientX, y: event.clientY };
			this.draw();
		},
		onUp() {
			this.drag = null;
		},
		draw() {
			const canvas = this.$refs.canvas as HTMLCanvasElement | undefined;
			const ctx = canvas?.getContext?.("2d");
			if (!canvas || !ctx) {
				return;  // no 2D context (tests)
			}
			const { width, height } = this.size();
			const ratio = window.devicePixelRatio || 1;
			canvas.width = width * ratio;
			canvas.height = height * ratio;
			canvas.style.width = `${width}px`;
			canvas.style.height = `${height}px`;
			ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
			ctx.clearRect(0, 0, width, height);
			const tp = this.toolpath;
			const view = this.view;
			if (!tp || !view) {
				return;
			}
			const [lo, hi] = this.range;
			const s = tp.segments;
			ctx.lineCap = "round";
			for (let i = 0; i < s.x0.length; i++) {
				const obj = s.object[i];
				if (obj !== null && this.hiddenObjects.includes(obj)) {
					continue;
				}
				const travel = s.travel[i] === 1;
				if (travel && !this.showTravel) {
					continue;
				}
				const [ax, ay] = toScreen(view, s.x0[i], s.y0[i]);
				const [bx, by] = toScreen(view, s.x1[i], s.y1[i]);
				ctx.beginPath();
				ctx.moveTo(ax, ay);
				ctx.lineTo(bx, by);
				if (travel) {
					ctx.strokeStyle = "rgba(128,128,128,0.35)";
					ctx.lineWidth = 0.5;
				} else {
					ctx.strokeStyle = rampColor((s.flow[i] - lo) / (hi - lo));
					ctx.lineWidth = Math.max(1, Math.min(4, view.scale * 0.4));
				}
				ctx.stroke();
			}
			for (const p of this.measured) {
				const [px, py] = toScreen(view, p.x, p.y);
				ctx.beginPath();
				ctx.arc(px, py, 3, 0, Math.PI * 2);
				ctx.fillStyle = rampColor((p.flow - lo) / (hi - lo));
				ctx.strokeStyle = "rgba(0,0,0,0.6)";
				ctx.lineWidth = 0.75;
				ctx.fill();
				ctx.stroke();
			}
			for (const m of this.markers) {
				const [px, py] = toScreen(view, m.x, m.y);
				ctx.beginPath();
				ctx.arc(px, py, 7, 0, Math.PI * 2);
				ctx.strokeStyle = m.color;
				ctx.lineWidth = 2.5;
				ctx.stroke();
			}
		}
	}
});
</script>

<style scoped>
.qa-replay {
	position: relative;
	width: 100%;
	touch-action: none;
	cursor: grab;
	background: rgba(128, 128, 128, 0.06);
	border-radius: 4px;
}
.qa-replay__legend {
	position: absolute;
	right: 8px;
	bottom: 6px;
	display: flex;
	align-items: center;
	gap: 6px;
}
.qa-replay__ramp {
	display: inline-block;
	width: 120px;
	height: 8px;
	border-radius: 4px;
}
</style>
