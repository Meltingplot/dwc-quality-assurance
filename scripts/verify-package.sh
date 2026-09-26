#!/usr/bin/env bash
#
# Check that a built plugin ZIP is installable and complete.
#
# Usage: scripts/verify-package.sh <QualityAssurance-<ver>.zip> <DuetWebControl checkout>
#
# Shared by ci.yml, release.yml and scripts/ci-local.sh so the three check the same things.

set -euo pipefail

zip="$1"
dwc_dir="$(cd "$2" && pwd)"   # absolute: node would read a relative path as a module name

fail() { printf '[fail] %s\n' "$*" >&2; exit 1; }

[ -f "$zip" ] || fail "no plugin ZIP at $zip"
listing="$(unzip -Z1 "$zip")"

# DWC installs the ZIP itself, so plugin.json must sit at the root
grep -qx 'plugin.json' <<<"$listing" || fail "plugin.json is not at the ZIP root"
# DWC 3.7 (Vite) emits QualityAssurance-<hash>.js
grep -qE '^dwc/js/QualityAssurance-.*\.js$' <<<"$listing" || fail "ZIP carries no built DWC JS resource"
grep -qx 'dsf/qa-daemon.py' <<<"$listing" || fail "ZIP is missing the daemon"
if grep -q '__pycache__' <<<"$listing"; then fail "ZIP contains __pycache__ entries"; fi
if grep -qE '(^|/)tests/' <<<"$listing"; then fail "ZIP contains tests"; fi
# The builder writes a sourcemap archive next to the package; a nested ZIP is not installable
if grep -qE '\.zip$' <<<"$listing"; then fail "ZIP contains a nested *.zip entry"; fi

expected="$(node -p 'require(process.argv[1] + "/package.json").version.split(".").slice(0,2).join(".")' "$dwc_dir")"
unzip -p "$zip" plugin.json | node -e '
    let raw = "";
    process.stdin.on("data", (chunk) => { raw += chunk; });
    process.stdin.on("end", () => {
        const manifest = JSON.parse(raw);
        const expected = process.argv[1];
        const fail = (msg) => { console.error("[fail] " + msg); process.exit(1); };
        if (manifest.id !== "QualityAssurance") fail(`id is ${manifest.id}`);
        // "auto-major" in the repo; the builder writes the major.minor of its DWC checkout
        if (manifest.dwcVersion !== expected) fail(`dwcVersion is ${manifest.dwcVersion}, expected ${expected}`);
        if (manifest.sbcDsfVersion !== expected) fail(`sbcDsfVersion is ${manifest.sbcDsfVersion}, expected ${expected}`);
        if (manifest.sbcExecutable !== "qa-daemon.py") fail(`sbcExecutable is ${manifest.sbcExecutable}`);
        if (/auto/.test(manifest.version)) fail("version was not stamped");
        for (const key of ["dwcFiles", "dsfFiles"]) {
            if (!Array.isArray(manifest[key]) || manifest[key].length === 0) fail(`${key} is missing or empty`);
        }
        if (!manifest.dsfFiles.includes("qa-daemon.py")) fail("dsfFiles does not list qa-daemon.py");
        console.log(`version=${manifest.version} dwcVersion=${manifest.dwcVersion} sbcDsfVersion=${manifest.sbcDsfVersion}`);
    });
' "$expected"

printf '[ok] %s\n' "$zip"
