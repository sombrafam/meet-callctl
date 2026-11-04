#!/bin/sh
# Install meet-callctl.
#   sudo ./install.sh system   root part: udev rule + load uhid at boot
#   ./install.sh user          per-user part: program + systemd user service
set -eu
cd "$(dirname "$0")"
PREFIX_LIB="$HOME/.local/lib/meet-callctl"
BIN="$HOME/.local/bin/meet-callctl"
UNIT_DIR="$HOME/.config/systemd/user"

system() {
    [ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0 system" >&2; exit 1; }
    install -m 0644 packaging/70-meet-callctl.rules /etc/udev/rules.d/70-meet-callctl.rules
    install -m 0644 packaging/modules-load-meet-callctl.conf /etc/modules-load.d/meet-callctl.conf
    modprobe uhid
    udevadm control --reload-rules
    # Re-apply rules to the existing uhid node and any meet-callctl hidraw node.
    udevadm trigger --action=change --sysname-match=uhid
    udevadm trigger --action=change --subsystem-match=hidraw
    udevadm settle
    echo "System part installed. Check: getfacl /dev/uhid  (expect user:<you>:rw-)"
}

user() {
    [ "$(id -u)" -ne 0 ] || { echo "run the user part as your normal user" >&2; exit 1; }
    mkdir -p "$PREFIX_LIB" "$(dirname "$BIN")" "$UNIT_DIR" "$HOME/.local/share/doc/meet-callctl"
    rm -rf "$PREFIX_LIB/meet_callctl"
    cp -r meet_callctl "$PREFIX_LIB/"
    find "$PREFIX_LIB" -name __pycache__ -prune -exec rm -rf {} +
    # Quoted heredoc: nothing is interpolated into the Python source.
    # -I: ignore PYTHON* env vars, user site-packages and the current directory.
    cat > "$BIN" <<'WRAP'
#!/usr/bin/python3 -I
import os
import sys
sys.path.insert(0, os.path.expanduser("~/.local/lib/meet-callctl"))
from meet_callctl.cli import main
sys.exit(main())
WRAP
    chmod 0755 "$BIN"
    cp README.md "$HOME/.local/share/doc/meet-callctl/"
    install -m 0644 packaging/meet-callctl.service "$UNIT_DIR/meet-callctl.service"
    systemctl --user daemon-reload
    systemctl --user enable meet-callctl.service
    systemctl --user restart meet-callctl.service
    sleep 1
    systemctl --user --no-pager status meet-callctl.service | head -n 5 || true
    echo "User part installed: $BIN"
}

case "${1:-}" in
    system) system ;;
    user) user ;;
    *) echo "usage: sudo $0 system   then   $0 user" >&2; exit 2 ;;
esac
