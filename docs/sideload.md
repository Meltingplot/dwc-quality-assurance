# Testing on a CHX 350 without an image build

The CHX 350 image runs only the plugins it bundles (docs/image.md), so installing QA from
Settings › Plugins does not work: the image disables pip, DSF's own AppArmor handling is off in
favour of a fixed profile in which a plugin without its own block cannot read its own files, and
`/opt/dsf/sd` is part of the read-only root. `scripts/sideload.sh` installs QA the way the image
would bundle it, for a test round of a few seconds instead of release → image → flash.

## What it does on the SBC

`scripts/sideload.sh` builds the ZIP (`scripts/ci-local.sh build`), sends it with
`docs/apparmor-QualityAssurance.inc` and `scripts/sideload-remote.sh` through the HMI in one SSH
connection, checks their SHA-256 there and runs the remote part as root:

| Step | Persists across a reboot? |
|---|---|
| refuses unless the machine is idle (it restarts DSF) | – |
| `/opt/dsf/plugins/QualityAssurance.json` (manifest with `dsfFiles`/`dwcFiles`, as mp-dsf-seed writes it), `QualityAssurance/dsf/` | yes (slot-shared) |
| `QualityAssurance/venv`: a copy of Vigil's (same dsf-python 3.7.0b1 pin; pip is disabled on the image) | yes |
| web files `QualityAssurance-*` into `/opt/dsf/sd/www` (only QA's files; DWC's directories keep owner and mode) | yes |
| data directory `/opt/dsf/plugins/QualityAssurance-data` via `QA_DATA_DIR`, a systemd drop-in for `duetpluginservice` in `/run` | the data yes, the drop-in no |
| the image's plugin AppArmor profile plus QA's block (`docs/apparmor-QualityAssurance.inc`, the rules the image build adds) and the data directory, **enforcing**, loaded with `apparmor_parser -r -K` (no cache) | no |
| restarts DSF when the manifest or the environment is new (restarts Vigil and CHX350 too), starts QA, reports its status, its output and any AppArmor denials | – |

After a reboot the image's profile is enforced again without QA's block and the drop-in is gone:
QA does not start until the next `sideload.sh install`. `sideload.sh remove` stops QA, deletes its
code and web files (not its data) and loads the image's profile again.

The data directory is the one difference to a bundled QA: the image has no slot-shared path for
`/opt/dsf/sd/QualityAssurance` yet (docs/image.md §1).

## Use

```bash
scripts/sideload.sh              # build and install (or update) QA
scripts/sideload.sh --no-build   # install the newest ZIP in .ci-local/dist
scripts/sideload.sh status       # status, data directory, daemon output, AppArmor denials
scripts/sideload.sh remove
```

Hosts and host keys go into `.sideload.env` in the repository root (not committed):

```bash
QA_SIDELOAD_USER=meltingplot
QA_SIDELOAD_HMI=192.168.172.143            # the HMI; the SBC is 10.42.0.2 behind it
QA_SIDELOAD_HMI_HOSTKEY=SHA256:…           # pinned host keys (plink -hostkey)
QA_SIDELOAD_SBC_HOSTKEY=SHA256:…
QA_SIDELOAD_PLINK="/mnt/c/Program Files/PuTTY/plink.exe"   # WSL + Pageant; leave empty for OpenSSH -J
```

With `QA_SIDELOAD_PLINK` set, PuTTY's plink authenticates both hops through Pageant (for example
gpg-agent with PuTTY support and a hardware key): one install asks the key twice.

Then in the classic DWC (HMI › Service › "Klassisches DWC") start the plugin once under Settings ›
Plugins so its page loads, and set `timelapse.snapshotUrl` and `accelerometer.board` on its
Settings tab if they are not set yet (docs/image.md §4).

## Limits

- The image 0.1.0-rc.46 has no ffmpeg: frames are captured, the encoding fails and keeps them.
- The profile is loaded only into the kernel; whoever reboots the machine ends the test setup.
- QA's block widens `dsf_plugin_py` for every bundled plugin (all share it), exactly as it will in
  the image; nothing else is relaxed.
