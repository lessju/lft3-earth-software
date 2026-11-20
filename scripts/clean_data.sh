#!/usr/bin/env bash
# Remove data from previous observations and dumps under the station data directory.
#
#   scripts/clean_data.sh                     # dry run: list what would be removed, and its size
#   scripts/clean_data.sh --yes               # remove it
#   scripts/clean_data.sh --older-than 7      # only files last modified more than 7 days ago
#   scripts/clean_data.sh --only dumps --yes  # one category: spectra | hits | runs | dumps | captures
#
# Categories (all by default):
#   spectra   fine and coarse FBH5 spectra            <data>/spectra/
#   hits      search results                          <data>/hits/
#   runs      run records (logs, status, NIC counters) <data>/runs/
#   dumps     triggered raw-voltage dumps (GUPPI RAW)  <data>/dumps/  and <data>/triggers.log
#   captures  DDR / ADC captures from lft3-board       <data>/captures/
# Never touched: reference/ (parity baselines), bench/, plots/, synthetic/, and the directories themselves.
#
# Refuses to run while lft3 pipeline containers are running (files may still be open). The data directory is
# read from config/station.yaml (data_dir), or given with --data-dir.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
data_dir=""
older_than=""
only=""
yes=0

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y) yes=1 ;;
        --older-than) older_than="${2:?--older-than needs a number of days}"; shift ;;
        --only) only="${2:?--only needs a category}"; shift ;;
        --data-dir) data_dir="${2:?--data-dir needs a path}"; shift ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 2 ;;
    esac
    shift
done

if [ -z "$data_dir" ]; then
    data_dir="$(sed -n 's/^data_dir:[[:space:]]*\([^#[:space:]]*\).*/\1/p' "$here/config/station.yaml" | head -1)"
    data_dir="${data_dir:-/opt/lft3arth/data}"
fi
data_dir="$(realpath -e "$data_dir")"
case "$data_dir" in
    /|/home|/opt|/opt/lft3arth|"$HOME") echo "refusing: data_dir is $data_dir" >&2; exit 2 ;;
esac
if [ -n "$older_than" ] && ! [[ "$older_than" =~ ^[0-9]+$ ]]; then
    echo "--older-than takes whole days" >&2; exit 2
fi

declare -A paths=(
    [spectra]="spectra"
    [hits]="hits"
    [runs]="runs"
    [dumps]="dumps triggers.log"
    [captures]="captures"
)
categories="spectra hits runs dumps captures"
if [ -n "$only" ]; then
    [ -n "${paths[$only]:-}" ] || { echo "unknown category: $only (spectra hits runs dumps captures)" >&2; exit 2; }
    categories="$only"
fi

if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^lft3-'; then
    echo "lft3 pipeline containers are running; stop them first (lft3-pipeline stop):" >&2
    docker ps --format '  {{.Names}}' | grep 'lft3-' >&2
    exit 1
fi

age=()
[ -n "$older_than" ] && age=(-mtime "+$((older_than - 1))")

list=$(mktemp)
trap 'rm -f "$list"' EXIT
total=0
for c in $categories; do
    n=0; bytes=0
    for p in ${paths[$c]}; do
        target="$data_dir/$p"
        [ -e "$target" ] || continue
        while IFS= read -r -d '' f; do
            printf '%s\0' "$f" >> "$list"
            n=$((n + 1)); bytes=$((bytes + $(stat -c %s "$f")))
        done < <(find "$target" -type f "${age[@]}" -print0)
    done
    total=$((total + bytes))
    printf '%-9s %6d files  %10s\n' "$c" "$n" "$(numfmt --to=iec --suffix=B "$bytes")"
done
printf '%-9s %19s\n' "total" "$(numfmt --to=iec --suffix=B "$total")"
echo "data directory: $data_dir${older_than:+, files older than $older_than days}"

if [ ! -s "$list" ]; then
    echo "nothing to remove"; exit 0
fi
if [ "$yes" -ne 1 ]; then
    echo "dry run: nothing removed. Run again with --yes to remove these files."
    exit 0
fi

failed=0
xargs -0 rm -f -- < "$list" || failed=1
# remove directories left empty below the category roots (the roots themselves stay)
for c in $categories; do
    for p in ${paths[$c]}; do
        [ -d "$data_dir/$p" ] && find "$data_dir/$p" -mindepth 1 -type d -empty -delete 2>/dev/null || true
    done
done
left=0
while IFS= read -r -d '' f; do
    [ -e "$f" ] && left=$((left + 1))
done < "$list"
if [ "$left" -gt 0 ] || [ "$failed" -ne 0 ]; then
    echo "removed, but $left files could not be deleted (permissions? files written by a root container)" >&2
    exit 1
fi
echo "removed $(numfmt --to=iec --suffix=B "$total")"
