# Tapo Streamer

A desktop application for viewing and reviewing footage from TP-Link Tapo
cameras on Linux and Windows. Tapo provides a mobile app for live view and
event playback, but no equivalent desktop client — this project fills that
gap, giving you a native 4-camera grid, PTZ control, archive browsing, and a
motion-event timeline in one window.

Tapo Streamer is a live-viewing and archive-browsing front end. It does not
record anything itself — pair it with
[tapo-downloader](https://github.com/fangio10/tapo-downloader) to
continuously archive clips from your cameras to a NAS or local disk, then
point Tapo Streamer at that archive folder.

## Features

- **4-camera live grid** — simultaneous RTSP streams from up to 4 Tapo
  cameras, with per-camera high/low quality selection and optional audio.
- **Fullscreen & cycling** — click or use arrow keys to enter fullscreen on
  a stream and cycle between cameras.
- **PTZ control** — pan/tilt via ONVIF for supported cameras, with
  adjustable travel speed.
- **Archive browser** — browse clips downloaded by `tapo-downloader`
  (organized as `<archive_dir>/camN/YYYY-MM-DD/...`), with day-folder
  navigation, thumbnails, pagination, and per-clip watch progress
  (a partial-view "half-watched" indicator vs. a full "watched" mark).
- **Clip playback controls** — pause, rewind/forward 10s, replay, and a
  global playback speed control (1x/2x/4x/8x) that applies across all
  active clips at once.
- **Motion-triggered Events view** — automatically clusters clips across
  all 4 cameras into discrete "events" based on timestamp proximity, and
  plays them back in a synchronized, coordinated timeline (so clips from
  different cameras that overlap in time play together, with the gaps
  between them collapsed automatically). Events can be filtered by
  detection type (person, vehicle, pet, motion, etc., as tagged by your
  downloader), labeled, downloaded, or deleted.
- **Stream reliability** — automatic reconnect with configurable retry/backoff,
  automatic quality downgrade on sustained frame drops, and optional
  auto-revert back to HQ once the stream is stable again.
- **Sleep mode** — optionally stop live streams after the app has been
  unfocused/minimized for a configurable period, and resume automatically
  when you return to it.
- **Configurable UI** — font selection, clip-control button placement,
  window size persistence, and more via a tabbed settings dialog.

## Requirements

- Python 3
- [VLC](https://www.videolan.org/) installed on the system (the app uses
  `python-vlc`/`libvlc` for decoding — install the VLC desktop app or the
  `vlc`/`libvlc` package for your OS so the required shared libraries are
  present)
- Python packages: `python-vlc`, `Pillow` (`PIL`), and for PTZ support,
  an ONVIF client library (`onvif-zeep` or equivalent)
- One or more TP-Link Tapo cameras, each set up with an account/local
  credentials via the official Tapo app (Tapo Streamer connects to the
  cameras' RTSP streams directly, it doesn't talk to the Tapo cloud)
- (Optional, for archive/Events features) [tapo-downloader](https://github.com/fangio10/tapo-downloader)
  configured and running against the same cameras, writing into a folder
  structured as `archive_dir/cam1`, `archive_dir/cam2`, etc.

## Installation

```bash
git clone <this-repo-url>
cd tapo-streamer
pip install -r requirements.txt   # or: pip install python-vlc Pillow onvif-zeep
python3 tapo-streamer.py
```

Run with `--debug` to enable debug logging:

```bash
python3 tapo-streamer.py --debug
```

## Configuration

On first launch, Tapo Streamer creates a config directory:

- Linux: `~/.tapo-streamer/`
- Windows: `%APPDATA%\TapoStreamer\`

containing `config.json` (app settings), `watch_progress.json` (per-clip
resume positions), and an `events/` folder (cached per-day event scans).

Open the **Config** (gear) button in the app to set up:

- **Connection tab** — your camera account username/password, the archive
  directory (the same folder `tapo-downloader` writes to), and per-camera
  IP address plus HQ/audio/PTZ toggles.
- **General tab** — UI font, clip control position, PTZ travel speed,
  default playback speed, sleep mode timeout, and event-related options
  (motion-triggered events toggle, event clustering window, default event
  filter, editable event labels, events cache clearing).
- **Advanced tab** — retry/backoff behavior, quality-downgrade thresholds,
  no-frame timeout, and raw VLC parameters.

## Usage

- **Live grid**: the 4 camera feeds display in a grid. Click a stream (or
  use the on-screen fullscreen button, depending on your settings) to go
  fullscreen; `Up`/`Down` arrows or right-click also enter/exit fullscreen;
  `Left`/`Right` arrows cycle between cameras while fullscreen.
- **Archive mode**: click the disk icon to browse recorded clips per
  camera, organized by day. Click a clip to play it, with the usual
  transport controls (pause, rewind/forward, replay) and a shared
  global speed control.
- **Events**: click the lightning-bolt icon to open the event listing for
  a given day. Select which cameras' feeds to include per event, then hit
  play to watch them back in sync, or use the download/delete actions per
  event.

## Related project

- [tapo-downloader](https://github.com/fangio10/tapo-downloader) — handles
  the actual recording/archiving of Tapo camera clips to disk. Tapo
  Streamer is the viewer for that archive plus a live-streaming front end;
  it does not download or store video itself.

## Disclaimer

This is an independent, unofficial project and is not affiliated with or
endorsed by TP-Link.
