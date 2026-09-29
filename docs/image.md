# Bundling QualityAssurance with the CHX 350 image

Work order for the image build (`rpi-image-gen`, layer `meltingplot/layer/mp-dsf*`).
The image has a read-only root and runs only plugins it bundles: a plugin installed later from
Settings › Plugins cannot even read its own files (AppArmor profile `dsf_plugin_py`). So QA
reaches the machine only through the image. Read against rpi-image-gen `a35b0c0`
(2026-09-26); line references are to that commit.

**Status:** done in image 0.1.0-rc.49 (rpi-image-gen PR #56 @ 4a7f80b, 2026-09-28): QA 0.1.0-rc.1
pinned, `/opt/dsf/sd/QualityAssurance` slot-shared, the AppArmor block with the timelapse rules,
the settings of §4 seeded. The steps below stay as the reference for the next pin. A test machine
that ran a sideload before rc.49 keeps that sideload's data in
`/opt/dsf/plugins/QualityAssurance-data`; the bundled QA starts empty until the data is moved
(docs/sideload.md, "Data directory").

## 1. Pin the release (`meltingplot/layer/mp-dsf.d/plugins.list`)

Every QA release (`.github/workflows/release.yml`) publishes `QualityAssurance-<ver>.zip` and a
`.sha256`, and its release notes carry the line to add:

```
QualityAssurance <version> sha256:<hex> https://github.com/Meltingplot/dwc-quality-assurance/releases/download/v<version>/QualityAssurance-<version>.zip
```

What `mp-dsf-plugins` checks and QA satisfies:
- `id`/`version` as pinned; `dwcVersion`/`sbcDsfVersion` = the image's DSF (the builder writes
  `3.7`), an `sbcExecutable` (`qa-daemon.py`).
- `sbcPythonDependencies`: `dsf-python` only. The accelerometer spectra are computed in plain
  Python (no numpy), so the build never has to install further Python packages for QA.
- `sbcPackageDependencies`: `ffmpeg`, already in the package list of `mp-dsf.yaml`
  (added with "install plugin package dependencies from the layer, add ffmpeg", 4767c0f).
- No `rrfFiles`, no `sd/` files.

Also: licence in `LICENSES.txt` (MIT, Meltingplot GmbH), and `QualityAssurance` in
`skel/conf/plugins.txt` so it starts on its own.

**Data directory (required):** `/opt/dsf/sd` itself is part of the read-only erofs root; only the
subdirectories the image lists are bind mounts from `/persistent/shared` (rpi-image-gen
`mp-dsf.d/customize.overlay/etc/rpi-image-gen/slot-shared.d/dsf.conf`, mount points in
`mp-dsf-configure`, checked by `postbuild50-dsf-assert`; read 2026-09-27 at 5acba10). Without the
same three entries Vigil has — `Path=/opt/dsf/sd/QualityAssurance`, its mount point and the assert —
QA cannot create `/opt/dsf/sd/QualityAssurance` and records nothing. (`QA_DATA_DIR` overrides the
directory, e.g. for a test.) In the image since rc.49 (1d2fdee).

## 2. AppArmor block (`customize.overlay/etc/apparmor.d/opt.dsf.bin.DuetPluginService`, profile `dsf_plugin_py`)

The rules are in [apparmor-QualityAssurance.inc](apparmor-QualityAssurance.inc), indented to be
pasted into `dsf_plugin_py` after the CHX350 block: QA's code and manifest, its data directory
`/opt/dsf/sd/QualityAssurance/`, the job files (read), its endpoint sockets under
`/run/dsf/QualityAssurance/`, its own accelerometer CSVs, the height maps in `0:/sys` (read, for the
mesh's min/max and heights; added 2026-09-29), and for the timelapse the camera and ffmpeg (below). `scripts/sideload.sh` loads this same file on a test machine (docs/sideload.md);
on the lab machine (image 0.1.0-rc.46) QA started and ran idle with it in enforce mode without a
denial (2026-09-27); a print job with camera and accelerometer is still to run.

`sbcPermissions` now also names `readSystem` and `writeSystem` (the CSVs are in `0:/sys`);
`fileSystemAccess` covers them already in DSF's own check.
`codeInterceptionReadWrite` (2026-09-28) lets QA hold and resolve M240 for the timelapse
(dsf/qa_intercept.py). DCS enforces it; the interceptor uses the same `dcs.sock` connection, so the
AppArmor block needs nothing new (DuetPluginService AppArmorPermissionManager.cs:44-52, DSF v3.7-dev
@ cd3ae65f). The machine should carry an empty `/sys/M240.g` (chx350-config): without QA running,
RRF runs it instead of warning "M240: Command is not supported" at every layer.

### What the timelapse needs beyond that (decision for the image build)

The profile comment says "There is no exec rule at all, so a plugin can never start another
program", and it has no `network inet` rule. The timelapse (PLAN.md §5.11) needs both:

1. **Camera snapshot over HTTP** from the SBC to the HMI: `http://10.42.0.1/snapshot`
   (haproxy → motion). That needs `network inet stream,` in `dsf_plugin_py`. AppArmor cannot
   narrow it to one address; it allows TCP for all bundled plugins.
2. **ffmpeg / ffprobe** for AV1 encoding after the job, frame extraction for the analysis, and
   the frame count check. Proposal: `/usr/bin/ffmpeg ix, /usr/bin/ffprobe ix,` so they inherit
   `dsf_plugin_py` (same file rules: read the frames, write the video under
   `/opt/dsf/sd/QualityAssurance/`; libraries are covered by `/usr/lib/@{multiarch}/** mr`).
   QA pauses and resumes the encoder with SIGSTOP/SIGCONT while a job prints; with `ix` both
   processes carry the same label and the existing
   `signal (send, receive) peer=DuetPluginService//&DuetPluginService//dsf_plugin_py` covers it.
   QA lowers the priority itself, without running `nice`/`ionice`: its encoder thread sets nice 19
   (`setpriority`) and I/O class idle (`ioprio_set`) for itself, and the ffmpeg it starts inherits
   both (Linux keeps them per thread; `dsf/qa_timelapse.py`). Lowering a priority needs no
   capability (ioprio_set(2): the idle class needs none since Linux 2.6.25), so the profile needs
   no rule for it.
   Memory: SVT-AV1 at the camera's 1920×1080 peaks at about 0.55 GB with the default
   `timelapse.encoderThreads` 2 (0.93 GB with 4); a paused encoder keeps it during the print.

Without these two rules QA records everything else; a denied snapshot or encoder shows as a
`timelapse_failed` event and in `status.timelapse.lastError`.

## 3. Test

Build a prerelease image with the plugin and the build variable `IGconf_dsf_plugin_policy=complain`
(`./rpi-image-gen build … -- IGconf_dsf_plugin_policy=complain`; it is a build variable, not a boot
parameter, and the release gate refuses it for a final version — rpi-image-gen `mp-dsf.yaml`,
`hooks/prebuild05-mp-release-gate`, 2026-09-27), deliver it by Connect OTA or tryboot into the other
slot, print a short job, and collect `journalctl -b --grep 'apparmor="(ALLOWED|DENIED)"'`. Every ALLOWED line for
`dsf_plugin_py` that names QualityAssurance is a missing rule. Then boot in enforce mode and
check there is no DENIED line.

## 4. Settings on the CHX 350 (seeded by the image since rc.49)

Two settings depend on the machine, so their defaults are `null` and QA records neither timelapse
nor spectra until they are set (`status.timelapse.reason`, `status.accelerometer.reason` say so):

| Setting | CHX 350 |
|---|---|
| `timelapse.snapshotUrl` | `http://10.42.0.1/snapshot` (HMI camera through haproxy) |
| `accelerometer.board` | `60`: CAN address of the SZP (`60.i2c.lis`). The list index varies: the lab machine has the tool board's accelerometer (CAN 20) at 0 and the SZP's at 1 (2026-09-27) |

Either the operator sets them once on the QA page (classic DWC › Quality Assurance › Settings,
which also lists the configured accelerometers), or the image seeds
`/opt/dsf/sd/QualityAssurance/settings.json` on first boot with

```json
{ "timelapse": { "snapshotUrl": "http://10.42.0.1/snapshot" }, "accelerometer": { "board": 60 } }
```

(missing keys take their defaults; QA validates the file on start). The image does the latter since
rc.49 (rpi-image-gen c0d3cd2): mp-dsf-seed copies the file without overwriting, once, and waits for
the mount of `/opt/dsf/sd/QualityAssurance` first.
