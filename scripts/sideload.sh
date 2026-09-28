#!/bin/bash
# Test QA on a CHX 350 without an image build (docs/sideload.md): builds the plugin ZIP, sends it
# with the AppArmor block and scripts/sideload-remote.sh through the HMI to the SBC in one SSH
# connection, and runs the remote part there as root, through sudo of the SBC's administrator.
#
#   scripts/sideload.sh [install|status|remove] [--no-build] [--zip FILE]
#
# Hosts, accounts and host keys come from .sideload.env in the repository root (not committed):
#   QA_SIDELOAD_HMI_USER=meltingplot        # the HMI, only the jump
#   QA_SIDELOAD_USER=mpadmin                 # the SBC; the one account there that has sudo
#   QA_SIDELOAD_HMI=192.168.172.143          QA_SIDELOAD_HMI_HOSTKEY=SHA256:…
#   QA_SIDELOAD_SBC=10.42.0.2                QA_SIDELOAD_SBC_HOSTKEY=SHA256:…
#   QA_SIDELOAD_PLINK="/mnt/c/Program Files/PuTTY/plink.exe"   # WSL with Pageant; empty: OpenSSH
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
MODE=install
BUILD=1
ZIP=""
while [ $# -gt 0 ]; do
    case "$1" in
        install|status|remove) MODE=$1 ;;
        --no-build) BUILD=0 ;;
        --zip) ZIP=$2; BUILD=0; shift ;;
        *) echo "usage: $0 [install|status|remove] [--no-build] [--zip FILE]" >&2; exit 2 ;;
    esac
    shift
done

[ -f "$ROOT/.sideload.env" ] && . "$ROOT/.sideload.env"
: "${QA_SIDELOAD_USER:=mpadmin}" "${QA_SIDELOAD_HMI_USER:=meltingplot}"
: "${QA_SIDELOAD_SBC:=10.42.0.2}" "${QA_SIDELOAD_PLINK:=}"
: "${QA_SIDELOAD_HMI:?set it in .sideload.env}"

remote() {  # command on the SBC; stdin goes along
    if [ -n "$QA_SIDELOAD_PLINK" ]; then
        # PuTTY's plink talks to Pageant (e.g. gpg-agent with a hardware key); the proxy command
        # runs on Windows, hence its Windows path
        local win proxy
        win=$(wslpath -w "$QA_SIDELOAD_PLINK")
        proxy="\"$win\" -batch ${QA_SIDELOAD_HMI_HOSTKEY:+-hostkey $QA_SIDELOAD_HMI_HOSTKEY} -l $QA_SIDELOAD_HMI_USER -nc $QA_SIDELOAD_SBC:22 $QA_SIDELOAD_HMI"
        (cd /tmp && "$QA_SIDELOAD_PLINK" -batch ${QA_SIDELOAD_SBC_HOSTKEY:+-hostkey "$QA_SIDELOAD_SBC_HOSTKEY"} \
            -proxycmd "$proxy" -l "$QA_SIDELOAD_USER" "$QA_SIDELOAD_SBC" "$1")
    else
        ssh -J "$QA_SIDELOAD_HMI_USER@$QA_SIDELOAD_HMI" "$QA_SIDELOAD_USER@$QA_SIDELOAD_SBC" "$1"
    fi
}

bundle=$(mktemp -d)
trap 'rm -rf "$bundle"' EXIT
cp "$ROOT/scripts/sideload-remote.sh" "$ROOT/docs/apparmor-QualityAssurance.inc" "$bundle/"
if [ "$MODE" = install ]; then
    if [ "$BUILD" = 1 ]; then
        "$ROOT/scripts/ci-local.sh" build >/dev/null
    fi
    if [ -z "$ZIP" ]; then
        ZIP=$(ls -t "$ROOT"/.ci-local/dist/QualityAssurance-*.zip 2>/dev/null | grep -v srcmap | head -1 || true)
    fi
    [ -f "$ZIP" ] || { echo "no plugin ZIP (build with scripts/ci-local.sh build)" >&2; exit 1; }
    echo "sideloading $(basename "$ZIP")"
    cp "$ZIP" "$bundle/QualityAssurance.zip"
fi
sums=$(cd "$bundle" && sha256sum -- *)

# one connection: check for sudo, unpack into ~/qa-sideload, check the checksums, run the remote
# part as root. sudo -n, so an account without sudo fails at once instead of waiting for a
# password nobody can type: on the CHX 350 image only the administrator has sudo, never the
# Raspberry Pi Connect account meltingplot (rpi-image-gen PR #52, 2026-09-28).
tar -czf - -C "$bundle" . | remote "{ sudo -n true 2>/dev/null || { echo \"sideload: \$(id -un) has no sudo on the SBC; set QA_SIDELOAD_USER to its administrator (mpadmin)\" >&2; exit 1; }; } \
    && rm -rf ~/qa-sideload && mkdir -p ~/qa-sideload && tar -xzf - -C ~/qa-sideload \
    && cd ~/qa-sideload && printf '%s\n' '$sums' | sha256sum --quiet -c - \
    && sudo -n bash ~/qa-sideload/sideload-remote.sh $MODE"
