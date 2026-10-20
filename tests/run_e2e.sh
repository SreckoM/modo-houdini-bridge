#!/bin/bash
# End-to-end: build the fixture scene in headless Modo 16 and 17, send it,
# check the file in every installed Houdini.
#   tests/run_e2e.sh [output folder]
# Linux only. Set MODO16_CL / MODO17_CL to your modo_cl paths.
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$(mktemp -d /tmp/hb_e2e.XXXX)}"
mkdir -p "$OUT"
MODOS=("16:${MODO16_CL:-$HOME/Applications/Modo16.1v9.245592/modo/modo_cl}"
       "17:${MODO17_CL:-$HOME/Applications/modo/modo_cl}")
status=0
for entry in "${MODOS[@]}"; do
    ver="${entry%%:*}"; cl="${entry#*:}"
    [ -x "$cl" ] || { echo "skip Modo $ver"; continue; }
    file="$OUT/modo$ver.usdc"
    rm -f "$file"
    printf 'log.toConsole true\n@%s\napp.quit\n' "$HERE/modo_fixture.py" \
        | HB_OUT="$file" timeout 300 "$cl" 2>&1 | grep -E "Houdini bridge|rror"
    # each installed build once (/opt/hfs22.0 is a symlink to a build)
    for hython in $(readlink -f /opt/hfs2*/bin/hython 2>/dev/null | sort -u); do
        [ -x "$hython" ] || continue
        echo "== Modo $ver -> $(basename "$(dirname "$(dirname "$hython")")")"
        log="$("$hython" "$HERE/check_e2e.py" "$file" 2>&1)" || status=1
        echo "$log" | grep -E "^FAIL|failure"
    done
done
exit $status
