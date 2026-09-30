<template>
	<div ref="box" class="qa-replay" :class="{ 'qa-replay--fill': fill }" @wheel.prevent="onWheel" @pointerdown="onDown" @pointermove="onMove" @pointerup="onUp"
		@pointerleave="onUp" @dblclick="fit()" @contextmenu="onContextMenu">
		<canvas ref="canvas" />
		<div class="qa-replay__legend text-caption">
			<span>{{ formatNumber(range[0], 1) }}</span>
			<span class="qa-replay__ramp" :style="{ background: rampCss }" />
			<span>{{ formatNumber(range[1], 1) }} mm³/s</span>
		</div>
	</div>
</template>

<script lang="ts">
import { defineComponent, markRaw, type PropType } from "vue";

import type { PrintBounds, ToolpathAnswer } from "../core/api";
import { formatNumber } from "../core/format";
import { fitView, flowRange, pan, rampColor, toolpathBounds, zoomAt, type View } from "../core/replay";
import {
	drawStackLayer, drawToolpath, fitStack, isoCamera, levelOfDetail, lineWidth, orbit, Projection, Scratch, TOP_CAMERA, toolpathZ,
	type Camera, type StackLayer
} from "../core/stack";

export interface ReplayMarker {
	x: number;
	y: number;
	/** Height in the 3D stack (the event's layer) */
	z?: number;
	color: string;
	label: string;
	/** An event of a layer below the one shown (3D stack) */
	past?: boolean;
}

/** Stack vertices drawn while the view turns, zooms or pans; the whole stack once it rests */
const MOVING_POINTS = 150000;
const SETTLE_MS = 200;

/**
 * One layer's toolpath on a 2D canvas, coloured by commanded volumetric flow; the planned rate of the
 * executed moves as dots coloured on the same scale, events as rings. Wheel zooms, drag pans, double click fits.
 * With ``stack`` the view is 3D: the stack's layers below ``layer`` in grey under the layer, events of
 * those layers as smaller rings; drag turns and tilts, shift or right drag pans, double click returns to
 * the isometric view. The drawn stack is kept in an offscreen canvas that stepping up only adds to.
 */
export default defineComponent({
	props: {
		toolpath: { type: Object as PropType<ToolpathAnswer | null>, default: null },
		executed: { type: Array as PropType<Array<{ x: number; y: number; flow: number }>>, default: () => [] },
		markers: { type: Array as PropType<Array<ReplayMarker>>, default: () => [] },
		hiddenObjects: { type: Array as PropType<Array<number>>, default: () => [] },
		showTravel: { type: Boolean, default: false },
		/** Canvas height in px; 0 fills the height the parent gives the component */
		height: { type: Number, default: 480 },
		/** Layers of the 3D stack sorted by layer, null for the 2D view */
		stack: { type: Array as PropType<Array<StackLayer> | null>, default: null },
		/** Printed extent of the job file: the 3D view turns about its centre and fits it */
		stackBounds: { type: Object as PropType<PrintBounds | null>, default: null },
		/** The layer shown: the stack below it is drawn */
		layer: { type: Number, default: 0 }
	},
	data() {
		return {
			view: null as View | null,
			camera: TOP_CAMERA as Camera,
			drag: null as { x: number; y: number; turn: boolean } | null,
			/** Turned, zoomed or panned since the last fit: a later fit of new bounds would jump */
			touched: false,
			observer: null as ResizeObserver | null,
			/** Drawing state outside Vue's reactivity */
			raw: markRaw({
				cache: null as HTMLCanvasElement | null,
				key: "",
				/** First and highest layer in the cache */
				base: null as StackLayer | null,
				top: -Infinity,
				scratch: new Scratch(),
				moving: false,
				settle: null as ReturnType<typeof setTimeout> | null,
				frame: null as number | null
			})
		};
	},
	computed: {
		fill(): boolean {
			return this.height <= 0;
		},
		is3d(): boolean {
			return this.stack !== null;
		},
		range(): [number, number] {
			return this.toolpath ? flowRange(this.toolpath) : [0, 1];
		},
		rampCss(): string {
			return `linear-gradient(90deg, ${[0, 0.25, 0.5, 0.75, 1].map(rampColor).join(", ")})`;
		},
		/** Bounds the 3D view fits: the file's, else the layer's own */
		bounds3d(): PrintBounds | null {
			if (this.stackBounds) {
				return this.stackBounds;
			}
			const flat = this.toolpath ? toolpathBounds(this.toolpath, false) ?? toolpathBounds(this.toolpath) : null;
			return flat ? { ...flat, maxZ: toolpathZ(this.toolpath) ?? 0 } : null;
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
		executed() { this.draw(); },
		markers() { this.draw(); },
		hiddenObjects() { this.draw(); },
		showTravel() { this.draw(); },
		layer() { this.draw(); },
		stack(next: Array<StackLayer> | null, previous: Array<StackLayer> | null) {
			if ((next === null) !== (previous === null)) {
				this.fit();  // 2D ↔ 3D
			} else {
				this.draw();
			}
		},
		stackBounds() {
			// the file's extent arrives with the first stack answer; refit unless the view was moved
			if (this.is3d && !this.touched) {
				this.fit();
			}
		}
	},
	mounted() {
		if (typeof ResizeObserver !== "undefined") {
			// Filling its parent, the canvas first mounts at whatever height the layout has then:
			// fit again once it knows its size
			this.observer = new ResizeObserver(() => (this.fill ? this.fit(false) : this.draw()));
			this.observer.observe(this.$refs.box as Element);
		}
		this.fit();
	},
	beforeUnmount() {
		this.observer?.disconnect();
		if (this.raw.settle !== null) {
			clearTimeout(this.raw.settle);
		}
		if (this.raw.frame !== null) {
			cancelAnimationFrame(this.raw.frame);
		}
	},
	methods: {
		formatNumber,
		size(): { width: number; height: number } {
			const box = this.$refs.box as HTMLElement | undefined;
			return { width: Math.max(100, box?.clientWidth || 600), height: this.fill ? Math.max(100, box?.clientHeight || 0) : this.height };
		},
		/** Fit the view; in 3D back to the isometric camera unless ``resetCamera`` is false */
		fit(resetCamera = true) {
			const { width, height } = this.size();
			if (this.is3d) {
				const bounds = this.bounds3d;
				if (bounds && (resetCamera || this.camera === TOP_CAMERA)) {
					this.camera = isoCamera(bounds);
				}
				this.view = bounds ? fitStack(bounds, this.camera, width, height) : null;
			} else {
				const bounds = this.toolpath ? toolpathBounds(this.toolpath, false) ?? toolpathBounds(this.toolpath) : null;
				this.camera = TOP_CAMERA;
				this.view = bounds ? fitView(bounds, width, height) : null;
			}
			if (resetCamera) {
				this.touched = false;
			}
			this.draw();
		},
		/** While the view moves, draw a lighter stack; the whole one after ``SETTLE_MS`` of rest */
		moving() {
			this.touched = true;
			this.raw.moving = true;
			if (this.raw.settle !== null) {
				clearTimeout(this.raw.settle);
			}
			this.raw.settle = setTimeout(() => {
				this.raw.settle = null;
				this.raw.moving = false;
				this.draw();
			}, SETTLE_MS);
			this.requestDraw();
		},
		requestDraw() {
			if (typeof requestAnimationFrame !== "function") {
				this.draw();
			} else if (this.raw.frame === null) {
				this.raw.frame = requestAnimationFrame(() => {
					this.raw.frame = null;
					this.draw();
				});
			}
		},
		onWheel(event: WheelEvent) {
			if (!this.view) {
				return;
			}
			this.view = zoomAt(this.view, event.deltaY < 0 ? 1.2 : 1 / 1.2, event.offsetX, event.offsetY);
			this.moving();
		},
		onDown(event: PointerEvent) {
			// 3D: the left button turns, shift or another button pans
			this.drag = { x: event.clientX, y: event.clientY, turn: this.is3d && event.button === 0 && !event.shiftKey };
		},
		onMove(event: PointerEvent) {
			if (!this.drag || !this.view) {
				return;
			}
			const dx = event.clientX - this.drag.x, dy = event.clientY - this.drag.y;
			if (this.drag.turn) {
				this.camera = orbit(this.camera, dx, dy);
			} else {
				this.view = pan(this.view, dx, dy);
			}
			this.drag = { ...this.drag, x: event.clientX, y: event.clientY };
			this.moving();
		},
		onUp() {
			this.drag = null;
		},
		onContextMenu(event: MouseEvent) {
			if (this.is3d) {
				event.preventDefault();  // the right button pans
			}
		},
		/** The stack below the layer into the offscreen canvas: new layers on top, else all again */
		renderCache(below: Array<StackLayer>, projection: Projection, width: number, height: number, ratio: number): HTMLCanvasElement | null {
			const raw = this.raw;
			if (!raw.cache) {
				raw.cache = document.createElement("canvas");
			}
			const ctx = raw.cache.getContext?.("2d");
			if (!ctx) {
				return null;
			}
			const { camera, view } = projection;
			const key = [width, height, ratio, camera.azimuth, camera.elevation, camera.cx, camera.cy, camera.cz,
				view.scale, view.originX, view.originY, this.hiddenObjects.join(",")].join("|");
			if (key !== raw.key || below[0] !== raw.base || raw.top >= this.layer) {
				raw.cache.width = width * ratio;
				raw.cache.height = height * ratio;
				raw.key = key;
				raw.base = below[0] ?? null;
				raw.top = -Infinity;
			}
			ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
			ctx.lineCap = "round";
			ctx.lineJoin = "round";
			ctx.lineWidth = lineWidth(view);
			for (const layer of below) {
				if (layer.layer > raw.top) {
					drawStackLayer(ctx, layer, projection, this.hiddenObjects, raw.scratch);
					raw.top = layer.layer;
				}
			}
			return raw.cache;
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
			const view = this.view;
			if (!view) {
				return;
			}
			const projection = new Projection(this.camera, view);
			ctx.lineCap = "round";
			ctx.lineJoin = "round";
			if (this.stack) {
				const below = this.stack.filter((l) => l.layer < this.layer);
				if (this.raw.moving) {
					ctx.lineWidth = lineWidth(view);
					for (const layer of levelOfDetail(below, MOVING_POINTS)) {
						drawStackLayer(ctx, layer, projection, this.hiddenObjects, this.raw.scratch);
					}
				} else {
					const cache = this.renderCache(below, projection, width, height, ratio);
					if (cache) {
						ctx.drawImage(cache, 0, 0, width, height);
					}
				}
			}
			const tp = this.toolpath;
			if (!tp) {
				return;
			}
			const [lo, hi] = this.range;
			drawToolpath(ctx, tp, projection, [lo, hi], this.hiddenObjects, this.showTravel, this.raw.scratch);
			const z = toolpathZ(tp) ?? 0;
			for (const p of this.executed) {
				const [px, py] = projection.screen(p.x, p.y, z);
				ctx.beginPath();
				ctx.arc(px, py, 3, 0, Math.PI * 2);
				ctx.fillStyle = rampColor((p.flow - lo) / (hi - lo));
				ctx.strokeStyle = "rgba(0,0,0,0.6)";
				ctx.lineWidth = 0.75;
				ctx.fill();
				ctx.stroke();
			}
			// the layer's own events last, over the ones below
			for (const m of [...this.markers.filter((m) => m.past), ...this.markers.filter((m) => !m.past)]) {
				const [px, py] = projection.screen(m.x, m.y, m.z ?? z);
				ctx.beginPath();
				ctx.arc(px, py, m.past ? 5 : 7, 0, Math.PI * 2);
				ctx.strokeStyle = m.color;
				ctx.lineWidth = m.past ? 1.5 : 2.5;
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
/* The canvas takes no room of its own, so the parent alone decides the height */
.qa-replay--fill {
	height: 100%;
	min-height: 100px;
}
.qa-replay--fill canvas {
	position: absolute;
	inset: 0;
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
