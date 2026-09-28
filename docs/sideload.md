# Testing on a CHX 350 without an image build

The CHX 350 image runs only the plugins it bundles (docs/image.md), so installing QA from
Settings › Plugins does not work: the image disables pip, DSF's own AppArmor handling is off in
favour of a fixed profile in which a plugin without its own block cannot read its own files, and
`/opt/dsf/sd` is part of the read-only root. `scripts/sideload.sh` installs QA the way the image
bundles it, for a test round of a few seconds instead of release → image → flash.

Only on a beta or rc image. A final image locks its AppArmor policy once it has loaded it, until the
next boot, so the profile with QA's block cannot be loaded there, by root neither; `install`
refuses before it changes anything (rpi-image-gen PR #54, 2026-09-28). And only as the SBC's
administrator `mpadmin`, from the printer network: the Raspberry Pi Connect account `meltingplot`
has no sudo, and sshd refuses `mpadmin` from the SBC itself (rpi-image-gen PR #52, 2026-09-28).

## Two kinds of image

From image 0.1.0-rc.49 on the image bundles QA (rpi-image-gen PR #56 @ 4a7f80b, QA 0.1.0-rc.1;
2026-09-28), and the sideload tests a newer version over the bundled one until the next boot:

| | image up to rc.48 | image from rc.49 on |
|---|---|---|
| data directory | `/opt/dsf/plugins/QualityAssurance-data` through `QA_DATA_DIR` (a systemd drop-in in `/run`): `/opt/dsf/sd` is read-only and has no QA directory | `/opt/dsf/sd/QualityAssurance`, QA's default, slot-shared by the image; no `QA_DATA_DIR`, no drop-in |
| code in `/opt/dsf/plugins/QualityAssurance` | written by the sideload | mp-dsf-seed mounts the image's copy read-only over it (rpi-image-gen b1b8982); the sideload unmounts it and writes into the seeded copy underneath |
| AppArmor block | the sideload's only, plus the data directory | the image's and the sideload's side by side: the same rules twice (apparmor_parser 4.0.1 compiles that; checked against the rc.49 profile, 2026-09-28) plus whatever the tested version needs beyond them |
| after a reboot | no QA (the image's profile has no block for it) until the next `install` | the image's QA again: mp-dsf-seed replaces a plugin whose version differs and mounts the image's copy; `install` therefore refuses a ZIP with the image's own version |

The script tells the two apart by `/opt/dsf/sd/QualityAssurance` being a mount point.

## What it does on the SBC

`scripts/sideload.sh` builds the ZIP (`scripts/ci-local.sh build`), sends it with
`docs/apparmor-QualityAssurance.inc` and `scripts/sideload-remote.sh` through the HMI in one SSH
connection, checks their SHA-256 there and runs the remote part as root, through `sudo -n` of the
SBC account:

| Step | Persists across a reboot? |
|---|---|
| refuses unless the SBC account has sudo, the AppArmor policy is open (not a final image) and the machine is idle (it restarts DSF); refuses the image's own version and split data (below) | – |
| stops QA and waits until DSF reports it stopped; moves the data of an older sideload over (below) | the data yes |
| `/opt/dsf/plugins/QualityAssurance.json` (manifest with `dsfFiles`/`dwcFiles`, as mp-dsf-seed writes it), `QualityAssurance/dsf/` | up to rc.48 yes (slot-shared); from rc.49 on replaced by the image's |
| `QualityAssurance/venv`: the image's, or a copy of Vigil's (same dsf-python 3.7.0b1 pin; pip is disabled on the image) | yes |
| web files `QualityAssurance-*` into `/opt/dsf/sd/www`: an older sideload's go, the image's stay (DWC loads the ones the manifest lists); DWC's directories keep owner and mode | yes |
| the data directory of the table above | the data yes, the drop-in no |
| the image's plugin AppArmor profile plus QA's block (`docs/apparmor-QualityAssurance.inc`, the rules the image build adds), **enforcing**, loaded with `apparmor_parser -r -K` (no cache) | no |
| restarts DSF when the manifest or the environment is new (restarts Vigil and CHX350 too), starts QA, reports its status, its output and any AppArmor denials | – |

`sideload.sh remove` stops QA and loads the image's profile again. On an image with QA it puts the
image's QA back as mp-dsf-seed does at boot (copy, read-only mount); on an older one it deletes QA's
code and web files. The data stays either way.

## Data directory

After the update from rc.48 to rc.49 a sideload's data is still in
`/opt/dsf/plugins/QualityAssurance-data` (`/opt/dsf/plugins` is slot-shared and the boot copies
without `--delete`), while the bundled QA starts on an empty database in
`/opt/dsf/sd/QualityAssurance`: the job list looks reset (CHX350-002, 2026-09-28). The bundled QA
cannot reach the old directory itself; the image's AppArmor block names only its own. So the
sideload compares the two databases by job id (read as `dsf`, read-only) whenever the old
directory exists:

| Old directory | `install` | `status` |
|---|---|---|
| every job is in `/opt/dsf/sd/QualityAssurance` too, or it has no database | goes on | warns until it is deleted |
| jobs, and `/opt/dsf/sd/QualityAssurance` has none | moves them over once QA is stopped (DSF reports pid ≤ 0, no process has a `qa.db` open): the image's directory goes aside whole to `QualityAssurance-data.image-<time>` (a `qa.db-wal` left beside the copied database would be applied to it), the old content is copied in, settings included; both old directories stay | warns |
| jobs missing from `/opt/dsf/sd/QualityAssurance`, which has jobs of its own | refuses before it changes anything: QA cannot merge two databases | warns |

Split data needs a decision: keep one of the two directories' content (QA stopped, then as
above: the image's directory emptied, the kept content copied in, owner `dsf`). Delete
`QualityAssurance-data` and `QualityAssurance-data.image-*` once QA shows the jobs.

## Use

```bash
scripts/sideload.sh              # build and install (or update) QA
scripts/sideload.sh --no-build   # install the newest ZIP in .ci-local/dist
scripts/sideload.sh status       # status, data directories, daemon output, AppArmor denials
scripts/sideload.sh remove
```

Hosts, accounts and host keys go into `.sideload.env` in the repository root (not committed):

```bash
QA_SIDELOAD_HMI_USER=meltingplot           # the HMI, only the jump (default)
QA_SIDELOAD_USER=mpadmin                   # the SBC: its administrator (default)
QA_SIDELOAD_HMI=192.168.172.143            # the HMI; the SBC is 10.42.0.2 behind it
QA_SIDELOAD_HMI_HOSTKEY=SHA256:…           # pinned host keys (plink -hostkey)
QA_SIDELOAD_SBC_HOSTKEY=SHA256:…
QA_SIDELOAD_PLINK="/mnt/c/Program Files/PuTTY/plink.exe"   # WSL + Pageant; leave empty for OpenSSH -J
```

With `QA_SIDELOAD_PLINK` set, PuTTY's plink authenticates both hops through Pageant (for example
gpg-agent with PuTTY support and a hardware key): one install asks the key twice. The key has to be
listed for `meltingplot` on the HMI and in rpi-image-gen's `meltingplot/keys/mpadmin.keys` for the
SBC.

An SBC with an image up to 0.1.0-rc.48 has no `mpadmin`; there `meltingplot` still has sudo, so set
`QA_SIDELOAD_USER=meltingplot` for it.

On an image up to rc.48, start the plugin once in the classic DWC (HMI › Service › "Klassisches
DWC") under Settings › Plugins so its page loads, and set `timelapse.snapshotUrl` and
`accelerometer.board` on its Settings tab if they are not set yet. From rc.49 on the image seeds
both settings (docs/image.md §4) and enables the page in DWC's factory defaults, which only a
printer without DWC settings reads; on an updated one start the plugin once as above.

## Limits

- The image 0.1.0-rc.46 has no ffmpeg: frames are captured, the encoding fails and keeps them.
- The profile is loaded only into the kernel; whoever reboots the machine ends the test setup.
- QA's block widens `dsf_plugin_py` for every bundled plugin (all share it), exactly as it does in
  the image; nothing else is relaxed. From rc.49 on, a rule the tested version drops still holds
  through the image's block until the next boot.
- The script was run in a privileged trixie container against the rc.48 and rc.49 layouts (mounts,
  databases and web files real, DSF's API, systemctl and apparmor_parser stubbed; 2026-09-28), not
  yet on a printer with rc.49.
