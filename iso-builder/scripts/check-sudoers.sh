#!/bin/bash
#
# Author  : Sylvain Deguire (VA2OPS)
# Date    : May 2026
# Purpose : Compare sudoers.d rules between personal and general overlays
#           and optionally sync missing files from personal to general
#

PERSONAL="/home/va2ops/emcomm-tools/overlays/et-v2-personal/overlay/etc/sudoers.d"
GENERAL="/home/va2ops/emcomm-tools/overlays/et-v2-general/overlay/etc/sudoers.d"

echo "=== Sudoers diff: personal vs general ==="
echo ""

# Files only in personal
for f in "$PERSONAL"/*; do
    name=$(basename "$f")
    if [[ ! -f "$GENERAL/$name" ]]; then
        echo "[MISSING IN GENERAL] $name"
    fi
done

# Files only in general
for f in "$GENERAL"/*; do
    name=$(basename "$f")
    if [[ ! -f "$PERSONAL/$name" ]]; then
        echo "[MISSING IN PERSONAL] $name"
    fi
done

# Files in both — show diff
for f in "$PERSONAL"/*; do
    name=$(basename "$f")
    if [[ -f "$GENERAL/$name" ]]; then
        diff_out=$(diff "$f" "$GENERAL/$name")
        if [[ -n "$diff_out" ]]; then
            echo "[DIFFERS] $name"
            echo "$diff_out"
            echo ""
        else
            echo "[OK] $name"
        fi
    fi
done

echo ""
echo "=== Sync missing files from personal to general? ==="
for f in "$PERSONAL"/*; do
    name=$(basename "$f")
    if [[ ! -f "$GENERAL/$name" ]]; then
        read -rp "  Copy $name to general? [y/N] " ans
        if [[ "$ans" == "y" || "$ans" == "Y" ]]; then
            cp "$f" "$GENERAL/$name"
            echo "  ✓ Copied $name"
        fi
    fi
done

echo ""
echo "Done."
