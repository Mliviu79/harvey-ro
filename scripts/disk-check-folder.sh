#!/bin/sh
# Docker on WSL: run once from the WSL shell, in the checkout. The Linux
# disk is a file on a Windows drive (any drive, as Windows records it for
# this distro), and the container can't see that drive, so Harvey's
# free-space check reads one empty folder on it instead. This creates that
# folder (harvey-disk-check on the drive) and adds HARVEY_HOST_DRIVE_SOURCE
# to .env; one .env already sets is checked instead (an empty folder on that
# drive), never changed. Then: docker compose up -d harvey.
#
# It runs on the WSL side, not in the container, because only there are
# Windows' reg.exe and the drives visible. Needs python3 (any 3.7+), not
# Harvey's install: harvey/disk.py uses the standard library only.
set -eu
cd "$(dirname "$0")/.."
if ! command -v python3 >/dev/null 2>&1; then
    echo "This needs python3 (sudo apt install python3), or set it by hand: create one empty" >&2
    echo "folder on the Windows drive that holds your WSL disk and add" >&2
    echo "HARVEY_HOST_DRIVE_SOURCE=<that folder> to .env." >&2
    exit 1
fi
# (-I: nothing from harvey/ or the user's site-packages shadows the
# standard library.)
exec python3 -I harvey/disk.py .env
