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
- `sbcPythonDependencies`: `dsf-python` only. (numpy comes only with the postponed
  accelerometer phase; that will need the build to install Python packages into the plugin venv.)
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
```

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
   QA lowers the encoder's priority itself (nice 19 and I/O class idle via the `ioprio_set`
   syscall in the child), without running `nice`/`ionice`.

Without these two rules QA records everything else; the timelapse reports itself disabled in
`status` and a `timelapse_failed` event when a snapshot or the encoder is denied.

## 3. Test

Build an image with the plugin, boot with `dsf.plugin_policy=complain`, print a short job, and
collect `journalctl -b --grep 'apparmor="(ALLOWED|DENIED)"'`. Every ALLOWED line for
`dsf_plugin_py` that names QualityAssurance is a missing rule. Then boot in enforce mode and
check there is no DENIED line.
