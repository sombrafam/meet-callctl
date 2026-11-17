# meet-callctl

Mute Google Meet from a global shortcut on Linux without switching windows.

meet-callctl creates a virtual USB-style call-control device (like a headset's
mute button). Meet controls it the way it controls a real headset, so
`meet-callctl toggle` mutes and unmutes Meet even while another window has
focus. No keystrokes are sent and no windows are raised.

Tested with Google Meet in Chrome on Ubuntu 24.04 (KDE Plasma 5.27, X11).

## Requirements

- Linux with systemd and udev; Python 3.10+ (no extra packages)
- Chrome, Chromium or another Chromium-based browser (Firefox lacks WebHID).
  Ubuntu's Chromium snap is supported: the udev rule tags the device for the
  snap's device cgroup. Install the snap before `sudo ./install.sh system`, or
  re-run it afterwards. Flatpak browsers are not supported

## Install

```sh
sudo ./install.sh system   # once: device permissions (udev rule) + load uhid
./install.sh user          # installs ~/.local/bin/meet-callctl and starts the service
```

The service starts with your desktop session. Then check it:

```sh
meet-callctl status        # "... no browser connected" until you connect it in Meet
```

## Connect it in Meet (once per browser profile)

1. Join any meeting in Chrome.
2. Open **⋮ → Settings → Audio → Call control → Connect device**.
3. Pick **meet-callctl Virtual Call Control** and click **Connect**.

`meet-callctl status` now shows `in call, browser connected`.

## Add a global shortcut

Bind this command to a key:

```
/home/YOUR_USER/.local/bin/meet-callctl toggle --notify
```

- **KDE Plasma 5:** System Settings → Shortcuts → Custom Shortcuts → Edit →
  New → Global Shortcut → Command/URL. Set the command (Action tab) and the
  key (Trigger tab).
- **KDE Plasma 6 / GNOME:** Settings → Keyboard → Shortcuts → add a custom
  command.
- **Anything else:** any hotkey tool, e.g. xbindkeys or sxhkd.

Use the full path, because hotkey daemons often don't have `~/.local/bin` in
`PATH`. `--notify` shows a pop-up with the result; leave it out if you don't
want one.

Tip: a global **Ctrl+M** takes the key from every app (in terminals Ctrl+M is
Enter). Something like **Meta+M** avoids that.

## Commands

| Command | Does |
| --- | --- |
| `meet-callctl toggle` | toggle Meet's mute |
| `meet-callctl mute` / `unmute` | set it explicitly |
| `meet-callctl status` | show what Meet last reported |
| `meet-callctl find-device` | show the device's `/dev/hidrawN` |

Options: `--notify` (desktop pop-up), `--no-wait`, `--json`.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Meet confirmed the change |
| 1 | error |
| 2 | daemon not running |
| 3 | report sent, but Meet didn't confirm the change |

Logs: `journalctl --user -u meet-callctl -f`

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `daemon is not running` | `systemctl --user restart meet-callctl`, then check the logs |
| `permission denied opening /dev/uhid` | Run `sudo ./install.sh system`. `getfacl /dev/uhid` must list your user. Use the local desktop session, not SSH |
| Device not in Chrome's list | Restart Chrome after installing. Chromium snap: re-run `sudo ./install.sh system`, restart the daemon, and check that `udevadm info /dev/hidrawN` lists `snap_chromium_chromium` in `TAGS` |
| `No browser has the device open` | Keep the Meet tab open; check the device is still under *Connected devices* |
| `Meet has not reported its mute state yet` | Join the meeting first |
| `Meet did not confirm` (exit 3) | Disconnect other call-control devices in Meet; try the `headset` profile (below) |

To test the browser side without a meeting, serve the test page:

```sh
python3 -m http.server -d tools 8765 --bind 127.0.0.1
```

Open <http://localhost:8765/webhid-test.html>, click *Connect device*, then
*Start fake call*, and run `meet-callctl toggle`. Close the tab before using
Meet.

## Uninstall

```sh
./uninstall.sh user
sudo ./uninstall.sh system
```

Also remove the device in Meet (*Call control*) and in
`chrome://settings/content/hidDevices`, and delete the shortcut.

## How it works

- A user service creates a HID device through `/dev/uhid`. It uses the
  Telephony usage page (0x0B), which is what Meet's *Call control* accepts.
- `meet-callctl toggle` talks to the service through a private socket
  (`$XDG_RUNTIME_DIR/meet-callctl/`, readable only by you). The service then
  sends a Phone Mute report.
- Meet answers by setting the device's Mute LED. That is the only mute state
  the tool trusts: it never guesses, so muting in Meet's own UI stays in sync.
- The device has no keyboard function, so the OS microphone is never muted.

**Profiles**

| Profile | How it mutes | Tested with Meet |
| --- | --- | --- |
| `phone` (default) | absolute Phone Mute (sends the wanted state); no hook switch, so it can never hang up a call | yes |
| `headset` | relative Phone Mute (a 0→1→0 pulse toggles) plus a hook switch | no |

Layouts copied from [jfedor2/meet-mute-button][jfedor2] and
[xav-ie/virtual-headset][vh]. To switch profiles:

```sh
systemctl --user edit meet-callctl
# add:
#   [Service]
#   ExecStart=
#   ExecStart=%h/.local/bin/meet-callctl daemon --profile headset
```

**Permissions**

- The udev rule grants access through an ACL to the logged-in desktop user
  only, for `/dev/uhid` and this device's `/dev/hidraw*` node. No
  world-writable modes or extra groups.
- Access to `/dev/uhid` also lets your programs create other HID devices, the
  same power `/dev/uinput` gives.
- The service runs unprivileged, sandboxed and without network.
- Device ID: `1209:0001`, the [pid.codes test ID][pidcodes].

**Check your system:** some third-party installers add udev rules that make
every `/dev/hidraw*` world-writable, which exposes keyboards. Check with
`ls -l /dev/hidraw*`; expect `crw-------`.

## Development

```sh
python3 -m pytest -q        # unit + fake-kernel tests; real /dev/uhid tests run if accessible
bin/meet-callctl daemon -v  # run from the source tree in the foreground
```

[jfedor2]: https://github.com/jfedor2/meet-mute-button
[vh]: https://github.com/xav-ie/virtual-headset
[pidcodes]: https://pid.codes/1209/0001/
