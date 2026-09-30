/**
 * Fan lines in vibration spectra (docs/api.md "Spectra"): a fan's imbalance shows at its rotation
 * frequency, rpm / 60. On the CHX 350 the part-cooling blowers made the largest peak of nearly every
 * recording at 152-155 Hz (2026-09-30), which read like a resonance of the machine.
 */
import type { Spectrum, SpectrumFan } from "./api";

/** Hz per bin */
export function binWidth(spectrum: Pick<Spectrum, "sampling_rate" | "n_samples">): number {
	return spectrum.n_samples > 0 ? spectrum.sampling_rate / spectrum.n_samples : 0;
}

/** The fans whose line is in the spectrum (turning, below Nyquist) */
export function turningFans(spectrum: Spectrum): Array<SpectrumFan & { hz: number }> {
	return (spectrum.fans ?? []).filter((f): f is SpectrumFan & { hz: number } => f.hz !== null && f.amplitude !== null);
}

/** Fans that stood although driven (PWM > 0, 0 rpm) */
export function stalledFans(spectrum: Spectrum): Array<SpectrumFan> {
	return (spectrum.fans ?? []).filter((f) => f.hz === null && (f.pwm ?? 0) > 0);
}

/** The fan whose line holds the spectrum's peak (within two bins, where the Hann lobe of its tone ends), or null */
export function fanAtPeak(spectrum: Spectrum): SpectrumFan | null {
	if (spectrum.peak_hz === null) {
		return null;
	}
	const reach = 2 * binWidth(spectrum);
	return turningFans(spectrum).find((f) => Math.abs(f.hz - spectrum.peak_hz!) <= reach) ?? null;
}
