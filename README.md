# Elegoo Print Recorder

Every print on your Elegoo resin printer, recorded by its own camera on your Mac. Automatically.

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
| A print starts | Recording starts at layer 3 |
| The print ends | 2 minutes later it saves `2026-10-16 14.11 My Part.mp4` |
| Your Mac restarts | It starts again by itself |

## Good to know

- **The camera only sends video while a print is running.** Run `make check` during a print to see the feed working.
- **The printer allows 2 camera viewers** (Lychee, the Elegoo app, this recorder). If both are taken, close one.
- **Camera stuck as "full"?** The printer never frees a viewer that crashed. Turn the printer off and on while it's idle.
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
