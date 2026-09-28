<template>
	<div class="qa-page pa-2">
		<backend-banner :host="host" />
		<live-panel :frame="frame" :status="status" :connection="connection" />
		<v-tabs v-model="tab" density="compact">
			<v-tab value="jobs" prepend-icon="mdi-format-list-bulleted">{{ $t("plugins.QualityAssurance.tabs.jobs") }}</v-tab>
			<v-tab value="job" prepend-icon="mdi-file-chart-outline" :disabled="!selectedJob">{{ $t("plugins.QualityAssurance.tabs.job") }}</v-tab>
			<v-tab value="trends" prepend-icon="mdi-chart-line">{{ $t("plugins.QualityAssurance.tabs.trends") }}</v-tab>
			<v-tab value="settings" prepend-icon="mdi-cog-outline">{{ $t("plugins.QualityAssurance.tabs.settings") }}</v-tab>
		</v-tabs>
		<!-- the gap lives inside the window: v-window clips, and an outlined field's floating label sits above its box -->
		<v-window v-model="tab" class="pt-2">
			<v-window-item value="jobs">
				<job-list :api="api" :selected="selectedJob" :reload-key="jobsVersion" @select="openJob" />
			</v-window-item>
			<v-window-item value="job">
				<job-detail v-if="selectedJob" :api="api" :job-id="selectedJob" :reload-key="jobVersion" />
			</v-window-item>
			<v-window-item value="trends">
				<trends-view v-if="tab === 'trends'" :api="api" />
			</v-window-item>
			<v-window-item value="settings">
				<settings-form v-if="tab === 'settings'" :api="api" />
			</v-window-item>
		</v-window>
	</div>
</template>

<script lang="ts">
import { defineComponent, markRaw, type PropType } from "vue";

import BackendBanner from "./components/BackendBanner.vue";
import JobDetail from "./components/JobDetail.vue";
import JobList from "./components/JobList.vue";
import LivePanel from "./components/LivePanel.vue";
import SettingsForm from "./components/SettingsForm.vue";
import TrendsView from "./components/TrendsView.vue";
import { QaApi, type StatusAnswer } from "./core/api";
import { PLUGIN_ID } from "./core/backend";
import type { HostAdapter } from "./core/host";
import { LiveClient, type LiveFrame, type LiveState } from "./core/ws";
import { createHost } from "./host";

/** A running job's detail reloads on its layer changes, at most this often */
const RUNNING_RELOAD_MS = 30000;

/** QA page of the classic DWC: job list, job detail, trends, settings, live state */
export default defineComponent({
	components: { BackendBanner, JobDetail, JobList, LivePanel, SettingsForm, TrendsView },
	props: {
		host: { type: Object as PropType<HostAdapter>, default: () => createHost() }
	},
	data() {
		return {
			api: markRaw(new QaApi(this.host)),
			live: null as LiveClient | null,
			tab: "jobs",
			selectedJob: null as string | null,
			frame: null as LiveFrame | null,
			status: null as StatusAnswer["collector"],
			connection: "closed" as LiveState,
			jobsVersion: 0,
			jobVersion: 0,
			lastJobReload: 0,
			jobReloadTimer: null as ReturnType<typeof setTimeout> | null
		};
	},
	mounted() {
		this.live = markRaw(new LiveClient({
			url: () => this.host.webSocketUrl(`machine/${PLUGIN_ID}/live`),
			onFrame: (frame) => this.onFrame(frame),
			onState: (state) => { this.connection = state; },
			poll: async () => {
				const collector = (await this.api.status()).collector;
				if (collector?.layer !== this.status?.layer) {
					this.onLayer(collector?.currentJobId ?? null);
				}
				this.status = collector;
			}
		}));
		this.live.start();
	},
	beforeUnmount() {
		this.live?.stop();
		if (this.jobReloadTimer !== null) {
			clearTimeout(this.jobReloadTimer);
		}
	},
	methods: {
		openJob(id: string) {
			this.selectedJob = id;
			this.tab = "job";
			this.lastJobReload = Date.now();  // the detail loads it on its own
		},
		/** New layer of a running job: reload its open detail now, or once when 30 s since the last reload are over */
		onLayer(jobId: string | null) {
			if (jobId === null || jobId !== this.selectedJob || this.jobReloadTimer !== null) {
				return;
			}
			const wait = this.lastJobReload + RUNNING_RELOAD_MS - Date.now();
			if (wait <= 0) {
				this.reloadJob();
			} else {
				this.jobReloadTimer = setTimeout(() => this.reloadJob(), wait);
			}
		},
		reloadJob() {
			if (this.jobReloadTimer !== null) {
				clearTimeout(this.jobReloadTimer);
				this.jobReloadTimer = null;
			}
			this.lastJobReload = Date.now();
			this.jobVersion++;
		},
		onFrame(frame: LiveFrame) {
			this.frame = frame;
			if (frame.type === "hello" || frame.type === "status") {
				this.status = frame.status ?? (frame.type === "status" ? { state: frame.state, currentJobId: frame.currentJobId, lastJobId: frame.lastJobId, layer: frame.layer } : null);
			} else if (frame.type === "sample") {
				this.status = { state: "recording", currentJobId: frame.jobId, lastJobId: frame.jobId, layer: frame.layer };
			} else if (frame.type === "job") {
				this.status = { state: "idle", currentJobId: null, lastJobId: frame.jobId, layer: null };
				this.jobsVersion++;
				if (frame.jobId === this.selectedJob) {
					this.reloadJob();
				}
			} else if (frame.type === "layer") {
				this.onLayer(frame.jobId ?? null);
			} else if (frame.type === "event" && frame.event?.type === "job_start") {
				this.jobsVersion++;
			}
		}
	}
});
</script>
