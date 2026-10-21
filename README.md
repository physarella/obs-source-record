# Source Record for OBS Studio

An OBS Studio filter that records, streams or replay-buffers a single source
or scene on its own, next to (or instead of) OBS's main output.

This is a fork of [Source Record by Exeldro](https://github.com/exeldro/obs-source-record),
based on its master branch after 0.4.8. Exeldro wrote the plugin; this fork
adds the changes below on top.

## What this fork changes

New:

- **Manual record mode.** Filters set to *Manual (Source Record hotkeys)*
  record only while you switch Source Recording on, without starting OBS's
  own recording. Upstream issue #146.
- **Global hotkeys:** Start, Stop and Toggle Source Recording, and Split All
  Source Recordings. A Toggle entry is also in the Tools menu.
- **Stop and restart mid-stream.** Stop also pauses filters that follow
  streaming, recording or the virtual camera, so you can end a source
  recording during a stream and Start a new file without touching the stream.
  The pause ends on Start, or when streaming, recording or the virtual camera
  next starts.
- **Save Replay hotkey per source,** so Source Record replay buffers can be
  bound with [obs-wayland-hotkeys](https://github.com/leia-uwu/obs-wayland-hotkeys)
  on Wayland.

Fixed:

- The Split File hotkey, button and websocket request now work without
  Automatic File Splitting turned on.
- Files after a split keep the spaces in their names (#164).
- A filter no longer keeps rendering its source every frame after a
  recording ends: in the test harness one idle 1080p filter raised OBS's
  render time from 3.7 ms to 8.1 ms (#38, #135). Sources also stop counting
  as shown once nothing records them.
- Two filters with the same folder and filename pattern no longer write the
  same file; the second gets a " (2)" suffix (#181, #105).
- The fragmented_mp4 and fragmented_mov formats were written as plain MP4
  and MOV, so a crash lost the whole recording. They are now fragmented, as
  in OBS's own settings.
- Builds against OBS 32.2 (#186).

If OBS or the whole machine dies mid-recording, mkv, hybrid_mp4, hybrid_mov,
fragmented_mp4 and fragmented_mov keep everything up to the last fraction of
a second; plain mp4 and mov are lost. (Tested by killing OBS and its
obs-ffmpeg-mux helpers 20 seconds into a recording.)

Issue numbers refer to the [upstream tracker](https://github.com/exeldro/obs-source-record/issues).

## Install

Remove the original Source Record first; OBS cannot load both. Both register
the same filter, so your existing filters and their settings carry over.

Downloads are on the [Releases](https://github.com/physarella/obs-source-record/releases) page.

- **Windows:** run the installer, or extract the zip into your OBS folder
  (usually `C:\Program Files\obs-studio`).
- **Linux:** extract the tarball into `~/.config/obs-studio/plugins/`, so the
  plugin ends up at `~/.config/obs-studio/plugins/source-record/bin/64bit/source-record.so`.
  Needs OBS 30 or later. The Flatpak OBS cannot load it.
- **macOS:** the packages are not signed, so macOS blocks the first run; allow
  it under System Settings, Privacy & Security.
- **Nix:** add the flake as an input and use its package:

  ```nix
  inputs.obs-source-record.url = "github:physarella/obs-source-record";
  # ...
  programs.obs-studio.plugins = [ inputs.obs-source-record.packages.x86_64-linux.default ];
  ```

  That package is built against the OBS pinned in this flake's lock.
  `overlays.default` builds it against your own nixpkgs' OBS instead.

The Linux build is tested against OBS 32.1.2 by `tests/run.py`. The Windows
and macOS builds come from CI and have not been tested by hand.

## Hotkeys

Set them in OBS under Settings, Hotkeys.

| Hotkey | Scope |
| --- | --- |
| Start / Stop Source Recording | global |
| Toggle Source Recording | global |
| Split All Source Recordings | global |
| Source Record Enable / Disable | per source |
| Split File, Add Chapter Marker, Pause / Unpause | per source |
| Save Replay (Source Record) | per source |

On Wayland, OBS does not see keys while it is unfocused. With
obs-wayland-hotkeys installed, the global hotkeys appear under their own names
(for example `source_record.Toggle`) and the per-source ones with the source
name appended (`source_record.SaveReplay.Game_Capture`). KDE and GNOME bind
one key per action, which is why Toggle exists next to Start and Stop.

## Build

With Nix:

```sh
nix build                 # the plugin, against the OBS pinned in flake.lock
nix develop               # build tools, OBS, and the test tools
python3 tests/run.py      # integration tests (builds the plugin first)
```

`flake.lock` pins the OBS version the plugin is tested against. To move to a
newer OBS, run `nix flake update nixpkgs`, then the tests.

Without Nix, on Linux with OBS's development files installed:

```sh
cmake -S . -B build -DBUILD_OUT_OF_TREE=On
cmake --build build
```

`tests/run.py` starts a throwaway OBS on a virtual X display (Xvfb) with its
own config, drives it over obs-websocket, and checks the files it writes with
ffprobe. It also fails when OBS crashes or reports leaks on exit.

## License and credit

GPL-2.0, like the original. See [LICENSE](LICENSE).

Source Record is Exeldro's work. To support the original author:
https://www.paypal.me/exeldro
