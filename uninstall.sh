#!/bin/sh
# Remove meet-callctl.
#   ./uninstall.sh user          stop service, remove program (as your user)
#   sudo ./uninstall.sh system   remove udev rule and module autoload
set -eu
case "${1:-}" in
    user)
        systemctl --user disable --now meet-callctl.service 2>/dev/null || true
        rm -f "$HOME/.config/systemd/user/meet-callctl.service" "$HOME/.local/bin/meet-callctl"
        rm -rf "$HOME/.local/lib/meet-callctl" "$HOME/.local/share/doc/meet-callctl"
        systemctl --user daemon-reload
        echo "User part removed. Remove any shortcut that runs 'meet-callctl'." ;;
    system)
        [ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0 system" >&2; exit 1; }
        rm -f /etc/udev/rules.d/70-meet-callctl.rules /etc/modules-load.d/meet-callctl.conf
        udevadm control --reload-rules
        udevadm trigger --action=change --sysname-match=uhid
        echo "System part removed. The uhid ACL goes away now; the module stays loaded until reboot." ;;
    *) echo "usage: $0 user   then   sudo $0 system" >&2; exit 2 ;;
esac
