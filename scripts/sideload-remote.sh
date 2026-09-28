#!/bin/bash
# Runs on the CHX 350 SBC as root; scripts/sideload.sh sends it with the plugin ZIP and the AppArmor
# block and starts it. Installs QualityAssurance for testing without an image build, the way the
# image would bundle it:
#
#   install   stop QA; unpack it into /opt/dsf/plugins as mp-dsf-seed does for bundled plugins
#             (manifest with dsfFiles/dwcFiles, code, venv: Vigil's, which has the same dsf-python
#             pin, since the image disables pip); web files into /opt/dsf/sd/www; load the plugin
#             AppArmor profile with QA's block (docs/apparmor-QualityAssurance.inc, what the image
#             build adds) into the kernel, enforcing; start QA and report status and denials.
#             Refused on a final image, whose AppArmor policy is locked: beta and rc images only
#   status    the report only
#   remove    stop QA, delete its code and web files (not its data), load the image's profile again
#
# One difference to a bundled QA: /opt/dsf/sd is part of the read-only root, and the image has no
# slot-shared path for /opt/dsf/sd/QualityAssurance yet (docs/image.md §1), so QA_DATA_DIR points
# the data at /opt/dsf/plugins/QualityAssurance-data and the AppArmor block gets that directory.
#
# Nothing but the files outlives a reboot: the profile is loaded with -K (no cache written) and the
# systemd drop-in lives in /run, so the next boot enforces the image's policy again, without QA.
set -euo pipefail

ID=QualityAssurance
HERE=$(dirname "$(readlink -f "$0")")
PLUGINS=/opt/dsf/plugins
WWW=/opt/dsf/sd/www
DATA=$PLUGINS/$ID-data
PROFILE=/etc/apparmor.d/opt.dsf.bin.DuetPluginService
BLOCK=$HERE/apparmor-$ID.inc
DROPIN_DIR=/run/systemd/system/duetpluginservice.service.d
DROPIN=$DROPIN_DIR/qa-sideload.conf
LOCK=/sys/module/apparmor/parameters/lock_policy
MODE=${1:-install}

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
        pid=$(model_field "(m['plugins'].get('$ID') or {}).get('pid', -1)" 2>/dev/null || echo -1)
        if [ "${pid:--1}" -gt 0 ] 2>/dev/null; then
            echo "running, pid $pid"
            return 0
        fi
        sleep 1
    done
    echo "not running"
    return 1
}

report() {
    if policy_locked; then
        say "AppArmor policy locked until the next boot (final image), QA cannot be sideloaded here"
    fi
    say "plugin"
    wait_pid || true
    say "QA status (GET machine/$ID/status)"
    api GET "$ID/status" | python3 -m json.tool 2>/dev/null | head -60 || echo "(no answer)"
    say "data directory $DATA"
    ls -la "$DATA" 2>/dev/null | tail -n +2 || echo "(missing)"
    say "daemon output, last 3 minutes"
    journalctl -u duetpluginservice --since "-3min" --no-pager -o cat 2>/dev/null |
        grep -i -E "$ID|qa[-_ ]|Traceback|Error" | tail -40 || true
    say "AppArmor denials of plugin profiles, last 10 minutes"
    journalctl -k --since "-10min" --no-pager -o cat 2>/dev/null | grep 'apparmor="DENIED"' |
        grep -E 'dsf_plugin_py|dsf_launcher' | tail -40 || echo "(none)"
}

profile_with_qa() {
    # The image's profile with QA's block at the end of dsf_plugin_py, plus the sideload's data dir
    python3 - "$PROFILE" "$BLOCK" "$DATA" <<'PY'
import sys
profile, block, data = open(sys.argv[1]).read(), open(sys.argv[2]).read(), sys.argv[3]
if "QualityAssurance" in profile:
    sys.exit("the image's profile already has a QualityAssurance block: use the bundled plugin")
end = profile.rstrip().rfind("\n  }\n}")   # closing brace of dsf_plugin_py, then of the parent
if end < 0:
    sys.exit("unexpected profile layout")
extra = (f"    # sideload only: the data directory QA_DATA_DIR points at (/opt/dsf/sd is read-only)\n"
         f"    {data}/ rw,\n    {data}/** rwk,\n")
sys.stdout.write(profile[:end] + "\n\n" + block.rstrip() + "\n" + extra + profile[end:])
PY
}

do_install() {
    local zip=$HERE/$ID.zip stage before after restart=0
    [ -f "$zip" ] || die "no $zip"
    [ -f "$BLOCK" ] || die "no $BLOCK"
    policy_locked && die "the AppArmor policy is locked: this is a final image, and sideloading is for beta and rc images only"
    require_idle

    say "stop $ID"
    api POST stopPlugin "$ID" >/dev/null

    say "unpack"
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
    find "$WWW" -name "$ID-*" -type f -delete
    (cd "$stage/dwc" && find . -type f) | while read -r file; do
        command install -o dsf -g dsf -m 0644 "$stage/dwc/$file" "$WWW/$file"  # coreutils, not do_install
    done
    mkdir -p "$DATA"
    chown -R dsf:dsf "$PLUGINS/$ID" "$PLUGINS/$ID.json" "$DATA"
    chmod 2770 "$DATA"
    rm -rf "$stage"

    say "AppArmor: image profile + $ID block, enforcing, kernel only (-K)"
    profile_with_qa > "$HERE/profile.qa"
    apparmor_parser -r -K "$HERE/profile.qa"
    echo "loaded"

    if [ ! -f "$DROPIN" ]; then
        say "data directory: QA_DATA_DIR=$DATA for duetpluginservice (drop-in in /run)"
        mkdir -p "$DROPIN_DIR"
        printf '[Service]\nEnvironment=QA_DATA_DIR=%s\n' "$DATA" > "$DROPIN"
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
    say "stop and remove $ID (its data in $DATA stays)"
    api POST stopPlugin "$ID" >/dev/null
    rm -rf "$PLUGINS/$ID" "$PLUGINS/$ID.json"
    find "$WWW" -name "$ID-*" -type f -delete
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
