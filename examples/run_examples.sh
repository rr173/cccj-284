#!/bin/sh
# Reproducible walkthrough of the archive tool.  Run from anywhere:
#   sh examples/run_examples.sh
# It builds two archives in a temporary directory, runs both provenance
# directions, a cross-buffer example, and the verifier.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
tool="$here/../textarchive"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT INT TERM

echo "== 1. basic example (checked-in fragments) =="
"$tool" build "$here/basic/manifest.json" -o "$work/basic.tadb"
"$tool" info "$work/basic.tadb"
echo
echo "-- normalized text (doc: greeting)"
"$tool" text "$work/basic.tadb" greeting
echo
echo "-- normalized -> original, code points [0,4)"
"$tool" spans "$work/basic.tadb" greeting 0 4
echo
echo "-- original -> normalized, fragment tibetan-0a bytes [0,3)"
"$tool" hits "$work/basic.tadb" tibetan tibetan-0a 0 3
echo
echo "== 2. cross-buffer example (generated) =="
python3 "$here/cross_buffer.py" "$work/cross"
"$tool" build "$work/cross/manifest.json" -o "$work/cross.tadb" --working-memory 1024
"$tool" info "$work/cross.tadb"
echo
echo "-- the character split across a fragment boundary, code points [0,2)"
"$tool" spans "$work/cross.tadb" huge 0 2
echo
echo "-- the same character, found from the last byte of part-a"
"$tool" hits "$work/cross.tadb" huge part-a 0 1
echo
echo "-- and from the first byte of part-b"
"$tool" hits "$work/cross.tadb" huge part-b 0 1
echo
echo "== 3. verification (reopened archive, no source files needed) =="
"$tool" verify "$work/basic.tadb"
"$tool" verify "$work/cross.tadb"
