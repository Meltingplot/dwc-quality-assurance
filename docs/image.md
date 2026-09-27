# Bundling QualityAssurance with the CHX 350 image

Work order for the image build (`rpi-image-gen`, layer `meltingplot/layer/mp-dsf*`).
The image has a read-only root and runs only plugins it bundles: a plugin installed later from
Settings › Plugins cannot even read its own files (AppArmor profile `dsf_plugin_py`). So QA
reaches the machine only through the image. Read against rpi-image-gen `a35b0c0`
(2026-09-26); line references are to that commit.

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

## 2. AppArmor block (`customize.overlay/etc/apparmor.d/opt.dsf.bin.DuetPluginService`, profile `dsf_plugin_py`)

Files, following the Vigil block:

```
    # QualityAssurance, process data of every print job
    /opt/dsf/plugins/QualityAssurance.json r,
    /opt/dsf/plugins/QualityAssurance/** mr,
    owner /opt/dsf/plugins/QualityAssurance/dsf/__pycache__/ w,
    owner /opt/dsf/plugins/QualityAssurance/dsf/__pycache__/* rw,
    # SQLite database with WAL/SHM files and locks, backups, settings, G-code layer
    # index cache, timelapse frames and videos
    /opt/dsf/sd/QualityAssurance/ rw,
    /opt/dsf/sd/QualityAssurance/** rwk,
    # job files: CRC32, slicer settings at the end of the file, toolpath for the replay
    /opt/dsf/sd/gcodes/ r,
    /opt/dsf/sd/gcodes/** r,
    # its HTTP endpoints (one of them is the WebSocket `live`)
    /run/dsf/QualityAssurance/{,**/} r,
    /run/dsf/QualityAssurance/** rw,
    # accelerometer: read and delete the CSVs of its own M956 recordings (qa-*.csv); RRF writes
    # them through DSF, so the plugin never creates files there
    /opt/dsf/sd/sys/accelerometer/ r,
    /opt/dsf/sd/sys/accelerometer/qa-*.csv rw,
```

`sbcPermissions` now also names `readSystem` and `writeSystem` (the CSVs are in `0:/sys`);
`fileSystemAccess` covers them already in DSF's own check.

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
   Memory: SVT-AV1 at the camera's 1984×1080 peaks at about 0.57 GB with the default
   `timelapse.encoderThreads` 2 (0.95 GB with 4); a paused encoder keeps it during the print.

Without these two rules QA records everything else; a denied snapshot or encoder shows as a
`timelapse_failed` event and in `status.timelapse.lastError`.

## 3. Test

Build an image with the plugin, boot with `dsf.plugin_policy=complain`, print a short job, and
collect `journalctl -b --grep 'apparmor="(ALLOWED|DENIED)"'`. Every ALLOWED line for
`dsf_plugin_py` that names QualityAssurance is a missing rule. Then boot in enforce mode and
check there is no DENIED line.

## 4. Settings on the CHX 350 (open)

QA's default `timelapse.snapshotUrl` is `null`, because the URL depends on the machine; without it
QA records no frames and `status.timelapse.reason` says so. On the CHX 350 it is
`http://10.42.0.1/snapshot`. Either the operator sets it once on the QA page (classic DWC ›
Quality Assurance › Settings), or the image seeds `/opt/dsf/sd/QualityAssurance/settings.json` on
first boot with

```json
{ "timelapse": { "snapshotUrl": "http://10.42.0.1/snapshot" } }
```

(missing keys take their defaults; QA validates the file on start). The accelerometer needs no
setting: QA uses the first one configured with M955, on the CHX the SZP's (CAN 60).
