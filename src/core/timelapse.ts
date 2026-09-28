/**
 * Timelapse helpers (PLAN.md §5.11): which frame shows a layer, where a `<video>` has to seek,
 * and the video as an object URL. The daemon takes a layer's frame when the next layer starts, so
 * frame n shows its layer finished; the last layer has none and shows the one before.
 */
import type { QaApi, TimelapseEntry, TimelapseMeta } from "./api";

/** Entries with a frame, in capture order */
function framed(meta: TimelapseMeta | null | undefined): Array<TimelapseEntry & { frame: number }> {
	return (meta?.layers ?? []).filter((e): e is TimelapseEntry & { frame: number } => e.frame !== null);
}

/**
 * The frame that shows ``layer``: its own (the latest if it was taken twice), else the latest of
 * an earlier layer (a skipped or failed snapshot). ``layer`` in the answer is the one it shows.
 */
export function frameForLayer(meta: TimelapseMeta | null | undefined, layer: number): { layer: number; frame: number } | null {
	let best: { layer: number; frame: number } | null = null;
	for (const e of framed(meta)) {
		if (e.layer <= layer && (best === null || e.layer > best.layer || (e.layer === best.layer && e.frame > best.frame))) {
			best = { layer: e.layer, frame: e.frame };
		}
	}
	return best;
}

/** Layers that have a frame, ascending, without repeats */
export function layersWithFrames(meta: TimelapseMeta | null | undefined): Array<number> {
	return [...new Set(framed(meta).map((e) => e.layer))].sort((a, b) => a - b);
}

/** A browser shows frame n from n/fps to (n + 1)/fps: seek into the middle of it */
export function seekTime(frame: number, fps: number): number {
	return (frame + 0.5) / fps;
}

/** The layer whose frame a video shows at ``time`` */
export function layerAtTime(meta: TimelapseMeta | null | undefined, time: number): number | null {
	const fps = meta?.fps;
	if (!fps) {
		return null;
	}
	const frame = Math.floor(time * fps + 1e-6);
	let layer: number | null = null;
	for (const e of framed(meta)) {
		if (e.frame <= frame) {
			layer = e.layer;
		}
	}
	return layer;
}

/** Still changing on the daemon: worth asking again */
export function isPending(meta: TimelapseMeta | null | undefined): boolean {
	return meta?.status === "capturing" || meta?.status === "queued" || meta?.status === "encoding";
}

/**
 * What to tell about the timelapse besides the frames: an i18n key under
 * ``plugins.QualityAssurance.timelapse`` with its parameters, null when the video is there
 */
export function timelapseNote(meta: TimelapseMeta | null | undefined): { key: string; params?: Record<string, string> } | null {
	switch (meta?.status) {
		case "none":
			return { key: "none", params: { reason: meta.reason ?? "—" } };
		case "capturing":
			return { key: "capturing" };
		case "queued":
		case "encoding":
			return { key: "encoding" };
		case "failed":
			return { key: "failed", params: { error: meta.error ?? "—" } };
		case "pruned":
			return { key: "pruned" };
		default:
			return null;
	}
}

let cached: { jobId: string; url: Promise<string> } | null = null;

/**
 * The job's video as an object URL, one job at a time: the Replay and Timelapse tabs of a job
 * share the download; loading another job's video releases the previous one.
 */
export function videoUrl(api: QaApi, jobId: string): Promise<string> {
	if (cached?.jobId === jobId) {
		return cached.url;
	}
	const previous = cached;
	const url = api.timelapseVideo(jobId).then((blob) => URL.createObjectURL(new Blob([blob], { type: "video/mp4" })));
	const entry = { jobId, url };
	cached = entry;
	url.catch(() => {
		if (cached === entry) {
			cached = null;
		}
	});
	previous?.url.then((u) => URL.revokeObjectURL(u), () => undefined);
	return url;
}

/** Drop the cached video (tests, job deleted) */
export function forgetVideo() {
	const previous = cached;
	cached = null;
	previous?.url.then((u) => URL.revokeObjectURL(u), () => undefined);
}
