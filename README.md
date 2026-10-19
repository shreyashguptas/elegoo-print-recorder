# Elegoo Print Recorder

Every print's timelapse from your Elegoo resin printer, copied to your Mac, server or NAS. Automatically.

<!-- demo GIF goes here -->

## Install

```bash
brew install uv ffmpeg
git clone https://github.com/shreyashguptas/elegoo-print-recorder
cd elegoo-print-recorder
make install
```

`make install` finds your printer, asks you to confirm its IP, checks the camera, and starts recording in the background. It needs your Mac password once.

Videos go to **Movies › Printer Recordings**, one `.mp4` per print, at the camera's full quality.

### On a Linux server (Debian/Ubuntu)

```bash
sudo apt install -y ffmpeg make git && curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/shreyashguptas/elegoo-print-recorder && cd elegoo-print-recorder
make install                                   # videos in ~/Printer Recordings
```

Saving to a NAS instead:

```bash
make nas SHARE=//192.168.1.10/recordings NAS_USER=me    # asks for the NAS password once
make install OUTPUT=/mnt/recordings
```

It runs as a systemd service: it starts at boot and restarts if it crashes. If the NAS is offline when a print ends, the video waits on the server and is copied over later.

## What it does

| When | What happens |
|---|---|
| A print finishes | The printer makes its own timelapse (~6 s, 1280×720, ~7 MB) |
| A minute later | It's copied to your recordings folder as `2026-10-16_14-11_My_Part_timelapse.mp4` |
| Your computer was off | It catches up on every timelapse it missed |

It never uses the camera and never deletes anything from the printer.

Want the full real-time video instead (every second of the print, ~2.5 GB)? Set `"mode": "video"` (or `"both"`) in the settings.

## Good to know

- **Stopped prints get no timelapse.** The printer only makes one when a print completes.
- Timelapse needs to be on in the printer's settings (it is by default).
- **The printer allows 2 camera viewers** (Lychee, the Elegoo app, this recorder). If both are taken, close one.
- **Camera stuck as "full"?** The printer never frees a viewer that crashed. Turn the printer off and on while it's idle.
- **Phone alerts:** put `"notify_url": "https://ntfy.sh/<a-random-topic>"` in the settings and subscribe to that topic in the free [ntfy](https://ntfy.sh) app. You get a ping if a print can't be recorded, if the camera is full between prints, and when each video is saved.
- **After `brew upgrade ffmpeg`, run `make install` again**, or recording stops.
- Keep the Mac awake and on the same network. If it restarts mid-print, you get two videos.

## Commands

| Command | |
|---|---|
| `make check` | Check the camera |
| `make status` | What's printing and recording right now |
| `make logs` | Live log |
| `make nas` | Linux: connect a NAS share |
| `make uninstall` | Remove it (your videos stay) |

Tested on a Saturn 4 Ultra (12K), firmware V1.5.2, on macOS 27 and Debian 13. Other Elegoo printers that use SDCP V3 should work.

MIT License
