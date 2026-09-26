<template>
	<div class="qa-page pa-2">
		<backend-banner :host="host" />
		<live-panel :frame="frame" :status="status" :connection="connection" />
		<v-tabs v-model="tab" density="compact" class="mb-2">
			<v-tab value="jobs" prepend-icon="mdi-format-list-bulleted">{{ $t("plugins.QualityAssurance.tabs.jobs") }}</v-tab>
			<v-tab value="job" prepend-icon="mdi-file-chart-outline" :disabled="!selectedJob">{{ $t("plugins.QualityAssurance.tabs.job") }}</v-tab>
			<v-tab value="trends" prepend-icon="mdi-chart-line">{{ $t("plugins.QualityAssurance.tabs.trends") }}</v-tab>
			<v-tab value="settings" prepend-icon="mdi-cog-outline">{{ $t("plugins.QualityAssurance.tabs.settings") }}</v-tab>
		</v-tabs>
		<v-window v-model="tab">
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
			jobVersion: 0
		};
	},
	mounted() {
		this.live = markRaw(new LiveClient({
			url: () => this.host.webSocketUrl(`machine/${PLUGIN_ID}/live`),
			onFrame: (frame) => this.onFrame(frame),
			onState: (state) => { this.connection = state; },
			poll: async () => { this.status = (await this.api.status()).collector; }
		}));
		this.live.start();
	},
	beforeUnmount() {
		this.live?.stop();
	},
	methods: {
		openJob(id: string) {
			this.selectedJob = id;
			this.tab = "job";
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
					this.jobVersion++;
				}
			} else if (frame.type === "event" && frame.event?.type === "job_start") {
				this.jobsVersion++;
			}
		}
	}
});
</script>
