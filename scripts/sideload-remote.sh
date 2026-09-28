#!/bin/bash
# Runs on the CHX 350 SBC as root; scripts/sideload.sh sends it with the plugin ZIP and the AppArmor
# block and starts it. Installs QualityAssurance for testing without an image build, the way the
# image bundles it:
#
#   install   stop QA; unpack it into /opt/dsf/plugins as mp-dsf-seed does for bundled plugins
#             (manifest with dsfFiles/dwcFiles, code, venv: the image's, or Vigil's, which has the
#             same dsf-python pin, since the image disables pip); web files into /opt/dsf/sd/www;
#             load the plugin AppArmor profile with QA's block (docs/apparmor-QualityAssurance.inc)
#             into the kernel, enforcing; start QA and report status and denials.
#             Refused on a final image, whose AppArmor policy is locked: beta and rc images only
#   status    the report only
#   remove    stop QA; on an image that bundles QA put the image's QA back, otherwise delete QA's
#             code and web files; never its data; load the image's profile again
#
# Two kinds of image (rpi-image-gen 4a7f80b, 2026-09-28):
# - From 0.1.0-rc.49 on the image bundles QA. /opt/dsf/sd/QualityAssurance is slot-shared, QA's
#   default data directory; the profile has QA's block; mp-dsf-seed mounts the image's copy of the
#   plugin read-only over /opt/dsf/plugins/QualityAssurance (b1b8982). install takes that mount
#   down and writes into the seeded copy underneath. The next boot's mp-dsf-seed replaces a plugin
#   whose version differs from the image's and mounts the image's copy again, so the ZIP must not
#   carry the image's version. QA's block is loaded once more beside the image's: the same rules
#   again, plus whatever the tested version needs beyond them.
# - Up to rc.48 /opt/dsf/sd is part of the read-only root and has no QA directory, so QA_DATA_DIR
#   points the data at /opt/dsf/plugins/QualityAssurance-data and the AppArmor block gets that
#   directory.
# After the update from rc.48 to rc.49 a sideload's data is still in QualityAssurance-data while the
# bundled QA starts on an empty database in /opt/dsf/sd/QualityAssurance. install moves the data
# over when the image's database has no job yet and refuses when both have jobs of their own
# (QA cannot merge databases); status warns as long as the old directory exists.
#
# Nothing but the files outlives a reboot: the profile is loaded with -K (no cache written) and the
# systemd drop-in lives in /run, so the next boot enforces the image's policy again.
set -euo pipefail

ID=QualityAssurance
HERE=$(dirname "$(readlink -f "$0")")
PLUGINS=/opt/dsf/plugins
WWW=/opt/dsf/sd/www
SKEL=/usr/share/meltingplot/dsf-skel  # the image's copy of the plugins it bundles (mp-dsf-seed)
DATA_IMAGE=/opt/dsf/sd/$ID            # slot-shared from image rc.49 on; QA's default
DATA_OLD=$PLUGINS/$ID-data            # the sideload's own before that, through QA_DATA_DIR
PROFILE=/etc/apparmor.d/opt.dsf.bin.DuetPluginService
BLOCK=$HERE/apparmor-$ID.inc
DROPIN_DIR=/run/systemd/system/duetpluginservice.service.d
DROPIN=$DROPIN_DIR/qa-sideload.conf
LOCK=/sys/module/apparmor/parameters/lock_policy
MODE=${1:-install}

if mountpoint -q "$DATA_IMAGE"; then
    DATA=$DATA_IMAGE
else
    DATA=$DATA_OLD
fi

say() { printf '\n== %s\n' "$*"; }
die() { echo "sideload: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"

# DSF's HTTP API on the SBC itself; the image sets no password, so connect yields a session
api() {  # method path [body]
    local key
    key=$(curl -s --max-time 5 "http://127.0.0.1/machine/connect" |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["sessionKey"])')
    curl -s --max-time 30 -X "$1" -H "X-Session-Key: $key" ${3:+--data-binary "$3"} "http://127.0.0.1/machine/$2" || true
    curl -s --max-time 5 -H "X-Session-Key: $key" "http://127.0.0.1/machine/disconnect" >/dev/null || true
}

model_field() {  # a Python expression over the object model m
    api GET model | python3 -c "import json,sys; m=json.load(sys.stdin); print($1)"
}

plugin_pid() {  # QA's pid as DSF reports it, -1 when stopped
    model_field "(m['plugins'].get('$ID') or {}).get('pid', -1)" 2>/dev/null || echo -1
}

manifest_version() {  # the version in a plugin manifest, empty without one
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version", ""))' "$1" 2>/dev/null || true
}

bundled() {  # the image carries QA itself (from rc.49 on)
    [ -f "$SKEL/plugins/$ID.json" ]
}

require_idle() {
    local status
    status=$(model_field 'm["state"]["status"]')
    case "$status" in
        idle|off|halted) ;;
        *) die "the machine is '$status'; restarting DSF now would disturb it" ;;
    esac
}

# A final CHX 350 image locks the AppArmor policy once it has loaded it, until the next boot: no
# profile can be loaded or replaced, by root neither (rpi-image-gen PR #54, 2026-09-28). Sideloading
# is for beta and rc images only, so install refuses there before it touches anything.
policy_locked() {
    [ "$(cat "$LOCK" 2>/dev/null)" = Y ]
}

wait_pid() {  # until QA runs (pid > 0), up to 30 s
    local pid
    for _ in $(seq 30); do
        pid=$(plugin_pid)
        if [ "${pid:--1}" -gt 0 ] 2>/dev/null; then
            echo "running, pid $pid"
            return 0
        fi
        sleep 1
    done
    echo "not running"
    return 1
}

wait_stopped() {  # until DSF reports QA stopped (pid <= 0), up to 30 s
    local pid
    for _ in $(seq 30); do
        pid=$(plugin_pid)
        [ "${pid:-1}" -le 0 ] 2>/dev/null && return 0
        sleep 1
    done
    return 1
}

open_handles() {  # the file descriptors of any process that has a qa.db of these directories open
    local dir
    for dir in "$@"; do
        find /proc/[0-9]*/fd -lname "$dir/qa.db*" 2>/dev/null || true
    done
}

# --- the sideload's data from before image rc.49 ------------------------------------------------

data_state() {  # "<state> <jobs in old> <jobs in image's> <old jobs the image's lacks>"
    # state: none (nothing to compare), redundant (the image's directory has every old job),
    # migrate (old jobs, the image's database has none), split (both have jobs of their own).
    # Read as dsf, so SQLite's -shm file, if it creates one, belongs to QA as always.
    [ "$DATA" = "$DATA_IMAGE" ] && [ -f "$DATA_OLD/qa.db" ] || { echo "none 0 0 0"; return 0; }
    runuser -u dsf -- python3 - "$DATA_OLD/qa.db" "$DATA_IMAGE/qa.db" <<'PY'
import os, sqlite3, sys

def jobs(path):
    if not os.path.exists(path):
        return set()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {row[0] for row in con.execute("SELECT id FROM jobs")}
    finally:
        con.close()

old, image = jobs(sys.argv[1]), jobs(sys.argv[2])
missing = old - image
state = "redundant" if not missing else "migrate" if not image else "split"
print(state, len(old), len(image), len(missing))
PY
}

data_warning() {  # what install and status say while the old directory exists
    local st state old image missing
    [ "$DATA" = "$DATA_IMAGE" ] && [ -e "$DATA_OLD" ] || return 0
    if ! st=$(data_state); then
        echo "WARNING: $DATA_OLD/qa.db or $DATA_IMAGE/qa.db cannot be read"
        return 0
    fi
    read -r state old image missing <<< "$st"
    case "$state" in
        none|redundant)
            echo "WARNING: $DATA_OLD, where sideloads before image rc.49 kept the data, still exists;"
            echo "$DATA has every job in it ($old). Delete it once you no longer need it: rm -rf $DATA_OLD" ;;
        migrate)
            echo "WARNING: $DATA_OLD holds $old job(s), $DATA none: QA shows an empty list."
            echo "scripts/sideload.sh install moves them over." ;;
        split)
            echo "WARNING: the data is split. $missing of the $old job(s) in $DATA_OLD are missing from"
            echo "$DATA, which has $image job(s) of its own. QA cannot merge two databases: keep one"
            echo "(docs/sideload.md, 'Data directory')." ;;
    esac
}

migrate_data() {  # the old directory's content into the image's, QA stopped
    local aside handles
    aside=$PLUGINS/$ID-data.image-$(date +%Y%m%d-%H%M%S)
    say "data: the sideload's jobs from $DATA_OLD into $DATA_IMAGE"
    [ "$(plugin_pid)" -le 0 ] 2>/dev/null || die "$ID still runs; the data stays where it is"
    handles=$(open_handles "$DATA_OLD" "$DATA_IMAGE")
    [ -z "$handles" ] || die "a process still has a database open ($handles); the data stays where it is"
    # The image's side (an empty database, the seeded settings) goes aside whole: a qa.db-wal
    # left beside the copied qa.db would be applied to it.
    mkdir "$aside"
    cp -a "$DATA_IMAGE/." "$aside/"
    find "$DATA_IMAGE" -mindepth 1 -delete
    cp -a "$DATA_OLD/." "$DATA_IMAGE/"
    find "$DATA_IMAGE" -mindepth 1 -exec chown dsf:dsf {} +
    echo "moved, the settings too; kept: $DATA_OLD (the old data), $aside (what the image's directory held)"
}

report() {
    local version
    if policy_locked; then
        say "AppArmor policy locked until the next boot (final image), QA cannot be sideloaded here"
    fi
    say "plugin"
    wait_pid || true
    if bundled; then
        version=$(manifest_version "$PLUGINS/$ID.json")
        if mountpoint -q "$PLUGINS/$ID"; then
            echo "the image's $ID ${version:-?}, read-only as mp-dsf-seed mounted it"
        else
            echo "sideloaded ${version:-?} over the image's $(manifest_version "$SKEL/plugins/$ID.json"), until the next boot"
        fi
    fi
    say "QA status (GET machine/$ID/status)"
    api GET "$ID/status" | python3 -m json.tool 2>/dev/null | head -60 || echo "(no answer)"
    if [ "$DATA" = "$DATA_IMAGE" ]; then
        say "data directory $DATA (the image's, slot-shared)"
    else
        say "data directory $DATA (the sideload's; this image has no $DATA_IMAGE)"
    fi
    ls -la "$DATA" 2>/dev/null | tail -n +2 || echo "(missing)"
    if [ "$DATA" = "$DATA_IMAGE" ] && [ -e "$DATA_OLD" ]; then
        say "old data directory $DATA_OLD"
        ls -la "$DATA_OLD" 2>/dev/null | tail -n +2
        data_warning
    fi
    say "daemon output, last 3 minutes"
    journalctl -u duetpluginservice --since "-3min" --no-pager -o cat 2>/dev/null |
        grep -i -E "$ID|qa[-_ ]|Traceback|Error" | tail -40 || true
    say "AppArmor denials of plugin profiles, last 10 minutes"
    journalctl -k --since "-10min" --no-pager -o cat 2>/dev/null | grep 'apparmor="DENIED"' |
        grep -E 'dsf_plugin_py|dsf_launcher' | tail -40 || echo "(none)"
}

profile_with_qa() {
    # The image's profile with QA's block at the end of dsf_plugin_py, plus the sideload's own data
    # directory on an image without QA's. An image that bundles QA has the block already; the copy
    # repeats those rules and adds what the tested version needs beyond them.
    local extra=""
    [ "$DATA" = "$DATA_OLD" ] && extra=$DATA_OLD
    python3 - "$PROFILE" "$BLOCK" "$extra" <<'PY'
import sys
profile, block, data = open(sys.argv[1]).read(), open(sys.argv[2]).read(), sys.argv[3]
end = profile.rstrip().rfind("\n  }\n}")   # closing brace of dsf_plugin_py, then of the parent
if end < 0:
    sys.exit("unexpected profile layout")
extra = ""
if data:
    extra = (f"    # sideload only: the data directory QA_DATA_DIR points at (/opt/dsf/sd is read-only)\n"
             f"    {data}/ rw,\n    {data}/** rwk,\n")
sys.stdout.write(profile[:end] + "\n\n" + block.rstrip() + "\n" + extra + profile[end:])
PY
}

prune_www() {  # QA's web files, except the image's QA's (its manifest lists them)
    # DWC loads the files a plugin's manifest lists (DuetWebControl v3.7-dev @ 1fb6a51,
    # src/plugins/index.ts:917, 2026-09-28), so the image's files beside a sideload's do nothing,
    # and the image's QA finds them again after remove or the next boot.
    python3 - "$WWW" "$ID" "$SKEL/plugins/$ID.json" <<'PY'
import json, os, sys
www, plugin, image = sys.argv[1:4]
keep = set()
if os.path.isfile(image):
    keep = {os.path.normpath(f) for f in json.load(open(image)).get("dwcFiles", [])}
for base, _dirs, names in os.walk(www):
    for name in names:
        path = os.path.join(base, name)
        if name.startswith(plugin + "-") and os.path.normpath(os.path.relpath(path, www)) not in keep:
            os.remove(path)
PY
}

do_install() {
    local zip=$HERE/$ID.zip stage before after restart=0 version st state=none
    [ -f "$zip" ] || die "no $zip"
    [ -f "$BLOCK" ] || die "no $BLOCK"
    policy_locked && die "the AppArmor policy is locked: this is a final image, and sideloading is for beta and rc images only"
    require_idle
    version=$(python3 -c 'import json,sys,zipfile; print(json.loads(zipfile.ZipFile(sys.argv[1]).read("plugin.json"))["version"])' "$zip")
    if bundled && [ "$version" = "$(manifest_version "$SKEL/plugins/$ID.json")" ]; then
        die "$ID $version is the version this image bundles; mp-dsf-seed puts the image's plugin back at boot only when the versions differ. Build from a commit past the tag"
    fi
    if [ "$DATA" = "$DATA_IMAGE" ] && [ -e "$DATA_OLD" ]; then
        st=$(data_state) || die "$DATA_OLD/qa.db or $DATA_IMAGE/qa.db cannot be read; nothing changed"
        read -r state _ <<< "$st"
        if [ "$state" = split ]; then
            data_warning >&2
            die "refused, nothing changed"
        fi
    fi

    say "stop $ID"
    api POST stopPlugin "$ID" >/dev/null
    wait_stopped || die "$ID did not stop"

    [ "$state" != migrate ] || migrate_data

    # the image's read-only copy (mp-dsf-seed) comes down; the sideload goes into the seeded copy
    # underneath, and the next boot mounts the image's again
    if mountpoint -q "$PLUGINS/$ID"; then
        umount "$PLUGINS/$ID" || die "the image's $ID cannot be unmounted; QA is stopped, nothing else changed"
        echo "the image's read-only $ID unmounted until the next boot"
    fi

    say "unpack $version"
    stage=$(mktemp -d)
    trap 'rm -rf "${stage:-}"' EXIT  # also on an abort; set -u, and stage is local
    python3 -m zipfile -e "$zip" "$stage"
    before=$(cat "$PLUGINS/$ID.json" 2>/dev/null || true)
    python3 - "$stage" "$PLUGINS/$ID.json" <<'PY'
import json, os, sys
stage, target = sys.argv[1], sys.argv[2]
manifest = json.load(open(os.path.join(stage, "plugin.json")))
def files(sub):
    root = os.path.join(stage, sub)
    return sorted(os.path.relpath(os.path.join(base, name), root)
                  for base, _dirs, names in os.walk(root) for name in names)
manifest.update({"dsfFiles": files("dsf"), "dwcFiles": files("dwc"), "sdFiles": [], "rrfFiles": []})
with open(target + ".tmp", "w") as handle:
    json.dump(manifest, handle, indent=2)
os.replace(target + ".tmp", target)
print("version", manifest["version"], "·", len(manifest["dsfFiles"]), "daemon files ·",
      len(manifest["dwcFiles"]), "web files")
PY
    after=$(cat "$PLUGINS/$ID.json")
    [ "$before" = "$after" ] || restart=1
    # DSF reads the manifests when it starts: one it has not seen needs a restart
    [ "$(model_field "'$ID' in m['plugins']")" = True ] || restart=1

    rm -rf "$PLUGINS/$ID/dsf"
    mkdir -p "$PLUGINS/$ID"
    cp -r "$stage/dsf" "$PLUGINS/$ID/dsf"
    if [ ! -d "$PLUGINS/$ID/venv" ]; then
        cp -a "$PLUGINS/Vigil/venv" "$PLUGINS/$ID/venv"
        restart=1
    fi
    # only QA's own files in www; the directories there belong to DWC and keep owner and mode
    prune_www
    (cd "$stage/dwc" && find . -type f) | while read -r file; do
        command install -o dsf -g dsf -m 0644 "$stage/dwc/$file" "$WWW/$file"  # coreutils, not do_install
    done
    chown -R dsf:dsf "$PLUGINS/$ID" "$PLUGINS/$ID.json"
    if [ "$DATA" = "$DATA_OLD" ]; then
        mkdir -p "$DATA"
        chown -R dsf:dsf "$DATA"
        chmod 2770 "$DATA"
    fi
    rm -rf "$stage"

    say "AppArmor: image profile + $ID block, enforcing, kernel only (-K)"
    profile_with_qa > "$HERE/profile.qa"
    apparmor_parser -r -K "$HERE/profile.qa"
    echo "loaded"

    if [ "$DATA" = "$DATA_OLD" ]; then
        if [ ! -f "$DROPIN" ]; then
            say "data directory: QA_DATA_DIR=$DATA for duetpluginservice (drop-in in /run)"
            mkdir -p "$DROPIN_DIR"
            printf '[Service]\nEnvironment=QA_DATA_DIR=%s\n' "$DATA" > "$DROPIN"
            systemctl daemon-reload
            restart=1
        fi
    elif [ -f "$DROPIN" ]; then
        say "data directory: the image's $DATA; the drop-in with QA_DATA_DIR goes"
        rm -f "$DROPIN"
        systemctl daemon-reload
        restart=1
    fi

    if [ "$restart" = 1 ]; then
        say "restart DSF (new manifest or environment; restarts every plugin)"
        systemctl restart duetcontrolserver
        for _ in $(seq 60); do
            curl -s --max-time 2 -o /dev/null "http://127.0.0.1/machine/connect" && break
            sleep 1
        done
        sleep 3
    fi

    say "start $ID"
    api POST startPlugin "$ID"
    echo
    report
}

do_remove() {
    require_idle
    say "stop $ID"
    api POST stopPlugin "$ID" >/dev/null
    wait_stopped || die "$ID did not stop"
    if bundled; then
        # what mp-dsf-seed does at boot: the image's copy in place, then mounted over it read-only
        say "the image's $ID again (its data in $DATA stays)"
        ! mountpoint -q "$PLUGINS/$ID" || umount "$PLUGINS/$ID"
        rm -rf "$PLUGINS/$ID"
        cp -a "$SKEL/plugins/$ID" "$PLUGINS/$ID"
        cp -a "$SKEL/plugins/$ID.json" "$PLUGINS/$ID.json"
        chown -R dsf:dsf "$PLUGINS/$ID" "$PLUGINS/$ID.json"
        mount --bind "$SKEL/plugins/$ID" "$PLUGINS/$ID"
        mount -o remount,bind,ro "$PLUGINS/$ID"
    else
        say "remove $ID (its data in $DATA stays)"
        rm -rf "$PLUGINS/$ID" "$PLUGINS/$ID.json"
    fi
    prune_www
    rm -f "$DROPIN"
    systemctl daemon-reload
    if policy_locked; then
        # locked at boot, before any sideload could load its profile: the image's is in force
        echo "the AppArmor policy is locked, the image's profile has been in force since boot"
    else
        apparmor_parser -r -K "$PROFILE"
        echo "the image's profile is loaded again"
    fi
    systemctl restart duetcontrolserver
    echo "DSF restarted"
}

case "$MODE" in
    install) do_install ;;
    status) report ;;
    remove) do_remove ;;
    *) die "usage: $0 install|status|remove" ;;
esac
