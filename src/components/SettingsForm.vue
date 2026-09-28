<template>
	<v-card variant="flat">
		<v-alert v-if="errors.length" type="error" variant="tonal" density="compact" class="mb-3">
			<div v-for="e in errors" :key="e">{{ e }}</div>
		</v-alert>
		<template v-if="form">
			<div class="text-subtitle-2 mb-1">{{ $t("plugins.QualityAssurance.settings.sampling") }}</div>
			<v-row density="compact">
				<v-col v-for="field in numberFields" :key="field.path" cols="12" sm="6" md="4">
					<v-text-field :model-value="get(field.path)" type="number" density="compact" variant="outlined"
						:label="$t(`plugins.QualityAssurance.settings.fields.${field.key}`)" :suffix="field.unit" :error-messages="fieldErrors(field.path)"
						@update:model-value="set(field.path, toNumber($event))" />
				</v-col>
			</v-row>

			<div class="text-subtitle-2 mb-1 mt-2">{{ $t("plugins.QualityAssurance.settings.chamber") }}</div>
			<v-row density="compact">
				<v-col cols="12" sm="4">
					<v-select :model-value="get('chamber.mode')" :items="chamberModes" density="compact" variant="outlined"
						:label="$t('plugins.QualityAssurance.settings.fields.chamberMode')" :error-messages="fieldErrors('chamber.mode')"
						:hint="get('chamber.mode') === 'auto' ? $t('plugins.QualityAssurance.settings.chamberAutoHint') : undefined" persistent-hint
						@update:model-value="set('chamber.mode', $event)" />
				</v-col>
				<v-col cols="12" sm="4">
					<v-text-field :model-value="get('chamber.index')" type="number" density="compact" variant="outlined"
						:label="$t('plugins.QualityAssurance.settings.fields.chamberIndex')" :disabled="get('chamber.mode') === 'auto'"
						:error-messages="fieldErrors('chamber.index')"
						@update:model-value="set('chamber.index', toIndex($event))" />
				</v-col>
				<v-col cols="12" sm="4">
					<v-text-field :model-value="get('chamber.autoSensorName')" density="compact" variant="outlined"
						:label="$t('plugins.QualityAssurance.settings.fields.chamberSensorName')" :error-messages="fieldErrors('chamber.autoSensorName')"
						@update:model-value="set('chamber.autoSensorName', $event)" />
				</v-col>
			</v-row>

			<div class="text-subtitle-2 mb-1 mt-2">{{ $t("plugins.QualityAssurance.settings.context") }}</div>
			<v-combobox :model-value="get('contextGlobals')" multiple chips closable-chips density="compact" variant="outlined"
				:label="$t('plugins.QualityAssurance.settings.fields.contextGlobals')" :error-messages="fieldErrors('contextGlobals')" @update:model-value="set('contextGlobals', $event)" />
			<v-switch :model-value="get('machineSignals.mfm')" color="primary" density="compact" hide-details
				:label="$t('plugins.QualityAssurance.settings.fields.mfm')" @update:model-value="set('machineSignals.mfm', !!$event)" />

			<div class="text-subtitle-2 mb-1 mt-2">{{ $t("plugins.QualityAssurance.settings.timelapse") }}</div>
			<v-row density="compact">
				<v-col cols="12" sm="3">
					<v-switch :model-value="get('timelapse.enabled')" color="primary" density="compact" hide-details
						:label="$t('plugins.QualityAssurance.settings.fields.timelapseEnabled')" @update:model-value="set('timelapse.enabled', !!$event)" />
				</v-col>
				<v-col cols="12" sm="9">
					<v-text-field :model-value="get('timelapse.snapshotUrl') ?? ''" density="compact" variant="outlined"
						placeholder="http://10.42.0.1/snapshot" :label="$t('plugins.QualityAssurance.settings.fields.snapshotUrl')"
						:error-messages="fieldErrors('timelapse.snapshotUrl')"
						@update:model-value="set('timelapse.snapshotUrl', $event ? $event : null)" />
				</v-col>
				<v-col v-for="field in timelapseFields" :key="field.path" cols="12" sm="4">
					<v-text-field :model-value="get(field.path)" type="number" density="compact" variant="outlined"
						:label="$t(`plugins.QualityAssurance.settings.fields.${field.key}`)" :suffix="field.unit" :error-messages="fieldErrors(field.path)"
						@update:model-value="set(field.path, field.nullable ? toIndex($event) : toNumber($event))" />
				</v-col>
				<v-col cols="12">
					<v-switch :model-value="get('timelapse.keepFramesOnFailure')" color="primary" density="compact" hide-details
						:label="$t('plugins.QualityAssurance.settings.fields.keepFramesOnFailure')"
						@update:model-value="set('timelapse.keepFramesOnFailure', !!$event)" />
				</v-col>
			</v-row>
			<div class="text-caption text-medium-emphasis">{{ $t("plugins.QualityAssurance.settings.timelapseM240") }}</div>
			<div v-if="get('timelapse.enabled') && timelapseReason" class="text-caption text-warning">
				{{ $t("plugins.QualityAssurance.settings.timelapseNotReady", { reason: timelapseReason }) }}
			</div>

			<div class="text-subtitle-2 mb-1 mt-2">{{ $t("plugins.QualityAssurance.settings.accelerometer") }}</div>
			<v-row density="compact">
				<v-col v-for="field in accelFields" :key="field.path" cols="12" sm="4">
					<v-text-field :model-value="get(field.path)" type="number" density="compact" variant="outlined"
						:label="$t(`plugins.QualityAssurance.settings.fields.${field.key}`)" :suffix="field.unit" :error-messages="fieldErrors(field.path)"
						@update:model-value="set(field.path, field.nullable ? toIndex($event) : toNumber($event))" />
				</v-col>
			</v-row>
			<div class="text-caption text-medium-emphasis">{{ accelHint }}</div>

			<v-alert v-if="saveErrors.length" type="error" variant="tonal" density="compact" class="mt-2">
				<div v-for="e in saveErrors" :key="e">{{ e }}</div>
			</v-alert>
			<v-alert v-if="saved" type="success" variant="tonal" density="compact" class="mt-2">
				{{ $t("plugins.QualityAssurance.settings.saved") }}
			</v-alert>
			<div class="d-flex ga-2 mt-2">
				<v-btn color="primary" :loading="saving" @click="save">{{ $t("plugins.QualityAssurance.settings.save") }}</v-btn>
				<v-btn variant="text" @click="load">{{ $t("plugins.QualityAssurance.settings.reset") }}</v-btn>
			</div>
		</template>
	</v-card>
</template>

<script lang="ts">
import { defineComponent, type PropType } from "vue";

import type { QaApi } from "../core/api";

const NUMBER_FIELDS = [
	{ key: "sampleIntervalS", path: "sampleIntervalS", unit: "s" },
	{ key: "ringBufferS", path: "ringBufferS", unit: "s" },
	{ key: "postTriggerS", path: "postTriggerS", unit: "s" },
	{ key: "temperatureK", path: "thresholds.temperatureK", unit: "K" },
	{ key: "filamentPercentPoints", path: "thresholds.filamentPercentPoints", unit: "%" },
	{ key: "vInPercent", path: "thresholds.vInPercent", unit: "%" },
	{ key: "phantomJumpK", path: "thresholds.phantomJumpK", unit: "K" },
	{ key: "filamentPercentWindowMinS", path: "filamentPercentWindowMinS", unit: "s" },
	{ key: "loadHigh", path: "heaterLoad.high", unit: "" },
	{ key: "loadLimit", path: "heaterLoad.limit", unit: "" },
	{ key: "retentionJobs", path: "retention.jobs", unit: "" },
	{ key: "retentionDays", path: "retention.days", unit: "d" }
];

const TIMELAPSE_FIELDS = [
	{ key: "minIntervalS", path: "timelapse.minIntervalS", unit: "s", nullable: false },
	{ key: "fps", path: "timelapse.fps", unit: "fps", nullable: false },
	{ key: "encoderThreads", path: "timelapse.encoderThreads", unit: "", nullable: true },
	{ key: "settleMs", path: "timelapse.settleMs", unit: "ms", nullable: false }
];

const ACCEL_FIELDS = [
	{ key: "accelBoard", path: "accelerometer.board", unit: "", nullable: true },
	{ key: "accelIntervalMin", path: "accelerometer.intervalMin", unit: "min", nullable: false },
	{ key: "accelSamples", path: "accelerometer.samples", unit: "", nullable: false }
];

/** The settings QA exposes in the UI; everything else stays as stored (the daemon validates) */
export default defineComponent({
	props: {
		api: { type: Object as PropType<QaApi>, required: true }
	},
	data() {
		return {
			form: null as Record<string, any> | null,
			errors: [] as Array<string>,
			saveErrors: [] as Array<string>,
			saving: false,
			saved: false,
			numberFields: NUMBER_FIELDS,
			timelapseFields: TIMELAPSE_FIELDS,
			accelFields: ACCEL_FIELDS,
			accelerometers: null as Array<{ index: number; port: string; board: number }> | null,
			timelapseReason: null as string | null
		};
	},
	computed: {
		accelHint(): string {
			if (this.accelerometers === null) {
				return this.$t("plugins.QualityAssurance.settings.accelHint");
			}
			return this.accelerometers.length
				? this.$t("plugins.QualityAssurance.settings.accelAvailable",
					{ list: this.accelerometers.map((a) => `${a.board} (${a.port})`).join(", ") })
				: this.$t("plugins.QualityAssurance.settings.accelNone");
		},
		chamberModes(): Array<{ title: string; value: string }> {
			return ["auto", "heater", "sensor"].map((value) => ({ title: this.$t(`plugins.QualityAssurance.settings.chamberModes.${value}`), value }));
		}
	},
	mounted() {
		this.load();
	},
	methods: {
		async load() {
			this.saved = false;
			this.saveErrors = [];
			try {
				const answer = await this.api.settings();
				this.form = JSON.parse(JSON.stringify(answer.settings));
				this.errors = answer.errors;
			} catch (e) {
				this.errors = [e instanceof Error ? e.message : String(e)];
			}
			await this.loadStatus();
		},
		async loadStatus() {
			try {
				const status = await this.api.status();
				// the accelerometers the machine has (M955), to pick a board from
				this.accelerometers = status.accelerometer.available ?? null;
				// why the saved timelapse settings do not produce a video (no URL, no ffmpeg)
				this.timelapseReason = status.timelapse?.reason ?? null;
			} catch {
				this.accelerometers = null;
				this.timelapseReason = null;
			}
		},
		get(path: string): any {
			return path.split(".").reduce((obj: any, key) => (obj ? obj[key] : undefined), this.form);
		},
		set(path: string, value: unknown) {
			const keys = path.split(".");
			let obj: any = this.form;
			for (const key of keys.slice(0, -1)) {
				obj = obj[key];
			}
			obj[keys[keys.length - 1]] = value;
			this.saved = false;
			this.saveErrors = this.saveErrors.filter((e) => !e.startsWith(`${path}: `));
		},
		/** The daemon's refusals for one field; it prefixes each with the setting's path */
		fieldErrors(path: string): Array<string> {
			return this.saveErrors.filter((e) => e.startsWith(`${path}: `)).map((e) => e.slice(path.length + 2));
		},
		toNumber(value: unknown): number | null {
			const n = Number(value);
			return value === "" || value === null || !Number.isFinite(n) ? null : n;
		},
		toIndex(value: unknown): number | null {
			const n = this.toNumber(value);
			return n === null ? null : Math.round(n);
		},
		async save() {
			if (!this.form) {
				return;
			}
			this.saving = true;
			this.saveErrors = [];
			try {
				const answer = await this.api.saveSettings(this.form);
				if (answer.saved) {
					this.form = JSON.parse(JSON.stringify(answer.settings));
					this.saved = true;
					await this.loadStatus();
				} else {
					this.saveErrors = answer.errors;  // the form keeps what was entered
				}
			} catch (e) {
				this.saveErrors = [e instanceof Error ? e.message : String(e)];
			} finally {
				this.saving = false;
			}
		}
	}
});
</script>
