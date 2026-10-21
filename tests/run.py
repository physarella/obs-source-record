#!/usr/bin/env python3
"""Integration tests: a throwaway OBS on a virtual X display, driven over
obs-websocket, with every result checked on disk.

Run from `nix develop`:  python3 tests/run.py [--plugin DIR] [case ...]

--plugin is an install prefix holding lib/obs-plugins and share/obs/obs-plugins
(a `nix build` result or a `cmake --install` prefix). Defaults to `nix build`.
"""

import argparse
import contextlib
import json
import logging
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import traceback

import obsws_python as obsws

REPO = pathlib.Path(__file__).resolve().parent.parent
WS_PORT = 4466
RTMP_PORT = 19350
X_DISPLAY = ":97"
CANVAS = (320, 240)

RECORD_MODE_NONE = 0
RECORD_MODE_ALWAYS = 1
RECORD_MODE_STREAMING = 2
RECORD_MODE_RECORDING = 3
RECORD_MODE_MANUAL = 6

PROFILE_INI = """\
[General]
Name=test

[Video]
BaseCX={w}
BaseCY={h}
OutputCX={w}
OutputCY={h}
FPSType=0
FPSCommon=30

[Output]
Mode=Simple
FilenameFormatting=%CCYY-%MM-%DD %hh-%mm-%ss main

[SimpleOutput]
FilePath={main_dir}
RecFormat2=mkv
RecQuality=Stream
StreamEncoder=x264
VBitrate=300
Preset=ultrafast

[Hotkeys]
source_record.Toggle={{"bindings":[{{"key":"OBS_KEY_F9"}}]}}
"""

USER_INI = """\
[General]
FirstRun=true

[Basic]
Profile=test
ProfileDir=test
SceneCollection=test
SceneCollectionFile=test
"""


class Failure(Exception):
    pass


def check(condition, message):
    if not condition:
        raise Failure(message)


def wait_until(predicate, timeout, interval=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class Obs:
    """One OBS process with its own config tree, recording into `out`."""

    def __init__(self, plugin_prefix, workdir):
        self.plugin_prefix = pathlib.Path(plugin_prefix)
        self.workdir = pathlib.Path(workdir)
        self.config = self.workdir / "config"
        self.out = self.workdir / "out"
        self.proc = None
        self.ws = None

    def write_config(self):
        obs_dir = self.config / "obs-studio"
        profile = obs_dir / "basic" / "profiles" / "test"
        websocket = obs_dir / "plugin_config" / "obs-websocket"
        for d in (profile, websocket, self.out / "main"):
            d.mkdir(parents=True, exist_ok=True)
        (obs_dir / "user.ini").write_text(USER_INI)
        (profile / "basic.ini").write_text(
            PROFILE_INI.format(w=CANVAS[0], h=CANVAS[1], main_dir=self.out / "main")
        )
        (websocket / "config.json").write_text(
            json.dumps(
                {
                    "alerts_enabled": False,
                    "auth_required": False,
                    "first_load": False,
                    "server_enabled": True,
                    "server_password": "",
                    "server_port": WS_PORT,
                }
            )
        )

    def start(self):
        self.write_config()
        env = dict(os.environ)
        env.update(
            DISPLAY=X_DISPLAY,
            QT_QPA_PLATFORM="xcb",
            XDG_CONFIG_HOME=str(self.config),
            OBS_PLUGINS_PATH=str(self.plugin_prefix / "lib" / "obs-plugins"),
            OBS_PLUGINS_DATA_PATH=str(self.plugin_prefix / "share" / "obs" / "obs-plugins"),
        )
        env.pop("WAYLAND_DISPLAY", None)
        obs_bin = pathlib.Path(os.environ["OBS_STUDIO"]) / "bin" / "obs"
        cmd = [
            "nice", "-n", "19",
            "nixGLIntel", str(obs_bin),
            "--multi", "--disable-shutdown-check", "--disable-updater",
            "--profile", "test", "--collection", "test",
        ]
        self.log = open(self.workdir / "stdout.txt", "w")
        self.proc = subprocess.Popen(cmd, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        connected = wait_until(self._connect, timeout=60, interval=1)
        check(connected, f"obs-websocket never came up; see {self.workdir}/stdout.txt")

    def _connect(self):
        if self.proc.poll() is not None:
            raise Failure(f"OBS exited during startup with {self.proc.returncode}")
        try:
            self.ws = obsws.ReqClient(host="127.0.0.1", port=WS_PORT, timeout=10)
            return True
        except (ConnectionRefusedError, OSError):
            return False

    def stop(self):
        """Returns the exit code. SIGINT, not SIGTERM: OBS 32.1.2 segfaults
        on SIGTERM even with no plugins loaded."""
        if self.ws:
            with contextlib.suppress(Exception):
                self.ws.disconnect()
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.dump_threads()
                self.proc.kill()
                self.proc.wait()
                raise Failure("OBS hung on shutdown")
        self.log.close()
        return self.proc.returncode

    def hotkey(self, name, source=None):
        """Fires a hotkey the way obs-wayland-hotkeys does: press, then release.
        `source` selects a source's own hotkey."""
        data = {"hotkeyName": name}
        if source:
            data["contextName"] = source
        self.request("TriggerHotkeyByName", data)

    def hotkey_names(self):
        return self.request("GetHotkeyList")["hotkeys"]

    def recording(self):
        return self.request("GetRecordStatus")["outputActive"]

    def streaming(self):
        return self.request("GetStreamStatus")["outputActive"]

    @contextlib.contextmanager
    def stream_to_local_server(self):
        """Streams to an ffmpeg RTMP listener that discards what it receives."""
        server = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-listen", "1",
             "-i", f"rtmp://127.0.0.1:{RTMP_PORT}/live/test", "-c", "copy", "-f", "null", "-"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(1)
            self.request(
                "SetStreamServiceSettings",
                {
                    "streamServiceType": "rtmp_custom",
                    "streamServiceSettings": {"server": f"rtmp://127.0.0.1:{RTMP_PORT}/live", "key": "test"},
                },
            )
            self.request("StartStream")
            check(wait_until(self.streaming, 10), "the stream did not start")
            yield
        finally:
            with contextlib.suppress(Exception):
                self.request("StopStream")
            wait_until(lambda: not self.streaming(), 10)
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()

    def showing(self, source):
        return self.request("GetSourceActive", {"sourceName": source})["videoShowing"]

    def render_ms(self):
        return self.request("GetStats")["averageFrameRenderTime"]

    def remaining_sources(self):
        """Sources OBS still held when it freed its context on exit."""
        for log in sorted((self.config / "obs-studio" / "logs").glob("*.txt")):
            for line in log.read_text(errors="replace").splitlines():
                if "source(s) were remaining" in line:
                    return int(line.split()[1])
        return 0

    def dump_threads(self):
        """Every thread's stack, for when OBS hangs."""
        out = self.workdir / "hang-backtrace.txt"
        with open(out, "w") as f:
            subprocess.run(["gdb", "-p", str(self.proc.pid), "-batch", "-ex", "thread apply all bt 30"],
                           stdout=f, stderr=subprocess.STDOUT, timeout=120)
        print(f"      OBS hung; thread stacks in {out}", flush=True)

    def leaks(self):
        """The leak count OBS logs on exit, or None if it never got that far."""
        for log in sorted((self.config / "obs-studio" / "logs").glob("*.txt")):
            for line in log.read_text(errors="replace").splitlines():
                if "Number of memory leaks:" in line:
                    return int(line.rsplit(":", 1)[1])
        return None

    def request(self, name, data=None):
        return self.ws.send(name, data, raw=True)

    def vendor(self, request_type, data=None):
        reply = self.request(
            "CallVendorRequest",
            {"vendorName": "source-record", "requestType": request_type, "requestData": data or {}},
        )
        return reply["responseData"]

    def add_color_source(self, name, size=CANVAS, visible=True, color=0xFF3366CC):
        self.request(
            "CreateInput",
            {
                "sceneName": "Scene",
                "inputName": name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": color, "width": size[0], "height": size[1]},
                "sceneItemEnabled": visible,
            },
        )

    def add_filter(self, source, name, **settings):
        defaults = {
            "record_mode": RECORD_MODE_NONE,
            "path": str(self.out / source),
            "filename_formatting": "%CCYY-%MM-%DD %hh-%mm-%ss " + source,
            "rec_format": "mkv",
            "encoder": "x264",
            "preset": "ultrafast",
            "bitrate": 300,
            "keyint_sec": 1,
        }
        defaults.update(settings)
        self.request(
            "CreateSourceFilter",
            {"sourceName": source, "filterName": name, "filterKind": "source_record_filter", "filterSettings": defaults},
        )

    def set_filter(self, source, name, **settings):
        self.request(
            "SetSourceFilterSettings",
            {"sourceName": source, "filterName": name, "filterSettings": settings, "overlay": True},
        )

    def files(self, subdir):
        d = self.out / subdir
        return sorted(p for p in d.iterdir() if p.is_file()) if d.exists() else []


def probe_duration(path):
    """Container duration in seconds, or None if ffprobe cannot read the file."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    duration = json.loads(result.stdout).get("format", {}).get("duration")
    return float(duration) if duration else None


def press_key(key):
    """Held long enough for OBS's hotkey thread, which polls every 25 ms."""
    env = dict(os.environ, DISPLAY=X_DISPLAY)
    subprocess.run(["xdotool", "keydown", key], env=env, check=True)
    time.sleep(0.2)
    subprocess.run(["xdotool", "keyup", key], env=env, check=True)


def probe_streams(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_entries",
         "stream=codec_type,width,height,nb_read_frames,start_time,duration:stream_tags", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout or "{}").get("streams", [])


def decodable_frames(path):
    """Video frames ffmpeg can decode; works on files still being written,
    which may not have a duration yet."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    streams = json.loads(result.stdout or "{}").get("streams", []) if result.returncode == 0 else []
    return int(streams[0].get("nb_read_frames", 0)) if streams else 0


def wait_for_files(obs, subdir, count, timeout=15):
    """Waits until `count` files exist and every one of them is readable."""

    def ready():
        files = obs.files(subdir)
        return len(files) >= count and all(probe_duration(f) for f in files)

    wait_until(ready, timeout)
    return obs.files(subdir)


# --- cases ------------------------------------------------------------------


def case_split_keeps_spaces(obs):
    """Upstream #164: split files lost the spaces of the filename format."""
    obs.add_color_source("Split Cam")
    obs.add_filter("Split Cam", "Source Record", split_file=True, max_time_sec=0, max_size_mb=0)
    obs.set_filter("Split Cam", "Source Record", record_mode=RECORD_MODE_ALWAYS)
    time.sleep(2.5)
    reply = obs.vendor("record_split", {"source": "Split Cam"})
    check(reply.get("success"), f"record_split failed: {reply}")
    time.sleep(2.5)
    obs.set_filter("Split Cam", "Source Record", record_mode=RECORD_MODE_NONE)
    files = wait_for_files(obs, "Split Cam", 2)
    names = [f.name for f in files]
    check(len(files) == 2, f"expected 2 files after one split, got {names}")
    for name in names:
        check(" Split Cam" in name, f"spaces were replaced in {name!r}")


def case_manual_split_without_auto_split(obs):
    """The split hotkey/button/request did nothing unless automatic splitting was on."""
    obs.add_color_source("Manual Cam")
    obs.add_filter("Manual Cam", "Source Record", split_file=False)
    obs.set_filter("Manual Cam", "Source Record", record_mode=RECORD_MODE_ALWAYS)
    time.sleep(2.5)
    obs.vendor("record_split", {"source": "Manual Cam"})
    time.sleep(2.5)
    obs.set_filter("Manual Cam", "Source Record", record_mode=RECORD_MODE_NONE)
    files = wait_for_files(obs, "Manual Cam", 2)
    check(len(files) == 2, f"expected 2 files after a manual split, got {[f.name for f in files]}")


def case_idle_filter_releases_source(obs):
    """Upstream #38/#135: after one recording the source stayed shown and
    rendered every frame, even hidden and with nothing recording."""
    big = (1920, 1080)
    obs.add_color_source("Hidden Cam", size=big, visible=False)
    obs.add_filter("Hidden Cam", "Source Record")
    time.sleep(2)
    check(not obs.showing("Hidden Cam"), "a hidden source with an idle filter is shown before any recording")
    idle_before = obs.render_ms()

    obs.set_filter("Hidden Cam", "Source Record", record_mode=RECORD_MODE_ALWAYS)
    check(wait_until(lambda: obs.showing("Hidden Cam"), 5), "the source is not shown while it records")
    time.sleep(2)
    obs.set_filter("Hidden Cam", "Source Record", record_mode=RECORD_MODE_NONE)
    wait_for_files(obs, "Hidden Cam", 1)

    released = wait_until(lambda: not obs.showing("Hidden Cam"), 5)
    time.sleep(3)
    idle_after = obs.render_ms()
    print(f"      render time idle: {idle_before:.2f} ms before, {idle_after:.2f} ms after recording")
    check(released, "the source is still shown after recording stopped")


def case_deleted_source_is_freed(obs):
    """Deleting a source mid-recording must finalize the file and free the source."""
    obs.add_color_source("Deleted Cam")
    obs.add_filter("Deleted Cam", "Source Record")
    obs.set_filter("Deleted Cam", "Source Record", record_mode=RECORD_MODE_ALWAYS)
    time.sleep(2)
    obs.request("RemoveInput", {"inputName": "Deleted Cam"})
    files = wait_for_files(obs, "Deleted Cam", 1)
    check(len(files) == 1, f"expected the recording to be finalized, got {[f.name for f in files]}")
    # Freed or not is only visible at exit: see remaining_sources().


def case_manual_mode_records_only_sources(obs):
    """Upstream #146: record the sources without OBS's own recording."""
    obs.add_color_source("Solo Cam")
    obs.add_filter("Solo Cam", "Source Record", record_mode=RECORD_MODE_MANUAL)
    obs.add_color_source("Bystander Cam")
    obs.add_filter("Bystander Cam", "Source Record", record_mode=RECORD_MODE_RECORDING)
    time.sleep(1.5)
    check(not obs.files("Solo Cam"), "a Manual filter recorded before Start")

    obs.hotkey("source_record.Start")
    check(wait_until(lambda: obs.files("Solo Cam"), 5), "Start did not begin a recording")
    time.sleep(2)
    obs.hotkey("source_record.Stop")
    check(len(wait_for_files(obs, "Solo Cam", 1)) == 1, "Stop did not finalize the recording")

    obs.hotkey("source_record.Toggle")
    check(wait_until(lambda: len(obs.files("Solo Cam")) == 2, 5), "Toggle did not begin a second recording")
    time.sleep(2)
    obs.hotkey("source_record.Toggle")
    files = wait_for_files(obs, "Solo Cam", 2)
    check(len(files) == 2, f"Toggle did not finalize the second recording: {[f.name for f in files]}")
    check(not obs.recording() and not obs.files("main"), "OBS's own recording ran")
    check(not obs.files("Bystander Cam"), "Start recorded a filter whose mode is Recording")


def case_saved_binding_survives_restart(obs):
    """A binding saved in the profile must work after OBS starts (cf. upstream
    #24). The key goes through X and OBS's own hotkey thread, not websocket."""
    obs.add_color_source("Key Cam")
    obs.add_filter("Key Cam", "Source Record", record_mode=RECORD_MODE_MANUAL)
    time.sleep(1)
    press_key("F9")
    check(wait_until(lambda: obs.files("Key Cam"), 5), "F9 (saved Toggle binding) did not start recording")
    time.sleep(2)
    press_key("F9")
    files = wait_for_files(obs, "Key Cam", 1)
    check(len(files) == 1, f"F9 did not stop the recording: {[f.name for f in files]}")


def case_save_replay_source_hotkey(obs):
    """A Source Record replay buffer needs a hotkey on its source: the one on
    its output shares OBS's name and obs-wayland-hotkeys keeps only one."""
    obs.add_color_source("Replay Cam")
    obs.add_filter("Replay Cam", "Source Record", replay_buffer=True, replay_duration=5,
                   replay_filename_formatting="%CCYY-%MM-%DD %hh-%mm-%ss replay")
    check(wait_until(lambda: "source_record.SaveReplay" in obs.hotkey_names(), 5), "no SaveReplay hotkey")
    time.sleep(3)
    obs.hotkey("source_record.SaveReplay", source="Replay Cam")
    files = wait_for_files(obs, "Replay Cam", 1)
    check(len(files) == 1 and "replay" in files[0].name, f"no replay was saved: {[f.name for f in files]}")
    obs.set_filter("Replay Cam", "Source Record", replay_buffer=False)


def case_split_all_hotkey(obs):
    for name in ("Split A", "Split B"):
        obs.add_color_source(name)
        obs.add_filter(name, "Source Record", record_mode=RECORD_MODE_MANUAL)
    obs.hotkey("source_record.Start")
    time.sleep(2.5)
    obs.hotkey("source_record.SplitAll")
    time.sleep(2.5)
    obs.hotkey("source_record.Stop")
    for name in ("Split A", "Split B"):
        files = wait_for_files(obs, name, 2)
        check(len(files) == 2, f"{name}: expected 2 files after Split All, got {[f.name for f in files]}")


def case_stop_and_restart_while_streaming(obs):
    """Stop the source recording mid-stream and start a new file, stream untouched."""
    obs.add_color_source("Stream Cam")
    obs.add_filter("Stream Cam", "Source Record", record_mode=RECORD_MODE_STREAMING)
    with obs.stream_to_local_server():
        check(wait_until(lambda: obs.files("Stream Cam"), 5), "Streaming mode did not record")
        time.sleep(2)
        obs.hotkey("source_record.Stop")
        check(len(wait_for_files(obs, "Stream Cam", 1)) == 1, "Stop did not finalize the recording")
        check(obs.streaming(), "Stop ended the stream")
        time.sleep(1)
        check(len(obs.files("Stream Cam")) == 1, "the recording restarted by itself while held")

        obs.hotkey("source_record.Start")
        check(wait_until(lambda: len(obs.files("Stream Cam")) == 2, 5), "Start did not begin a new file")
        time.sleep(2)
        check(obs.streaming(), "the stream stopped")
    obs.hotkey("source_record.Stop")
    files = wait_for_files(obs, "Stream Cam", 2)
    check(len(files) == 2, f"expected 2 files, got {[f.name for f in files]}")


def case_same_filename_does_not_collide(obs):
    """Upstream #181/#105: two filters with one folder and filename format
    wrote the same file, losing one recording."""
    shared = {"path": str(obs.out / "Shared"), "filename_formatting": "shared name"}
    for name in ("Twin A", "Twin B"):
        obs.add_color_source(name)
        obs.add_filter(name, "Source Record", **shared)
    for name in ("Twin A", "Twin B"):
        obs.set_filter(name, "Source Record", record_mode=RECORD_MODE_ALWAYS)
    time.sleep(3)
    for name in ("Twin A", "Twin B"):
        obs.set_filter(name, "Source Record", record_mode=RECORD_MODE_NONE)
    files = wait_for_files(obs, "Shared", 2)
    names = [f.name for f in files]
    check(names == ["shared name (2).mkv", "shared name.mkv"], f"expected two recordings, got {names}")
    for f in files:
        check((probe_duration(f) or 0) > 1, f"{f.name} is not a full recording")


def case_crash_safe_formats_readable_while_recording(obs):
    """A file that survives OBS dying can already be read while it is being
    written. fragmented_mp4/mov used to produce a plain MP4/MOV, which cannot."""
    formats = ["mkv", "fragmented_mp4", "fragmented_mov", "hybrid_mp4", "hybrid_mov"]
    for fmt in formats:
        obs.add_color_source(fmt)
        obs.add_filter(fmt, "Source Record", rec_format=fmt, filename_formatting=fmt, bitrate=4000)
        obs.set_filter(fmt, "Source Record", record_mode=RECORD_MODE_ALWAYS)
    time.sleep(6)
    unreadable = [fmt for fmt in formats if not (obs.files(fmt) and decodable_frames(obs.files(fmt)[0]))]
    for fmt in formats:
        obs.set_filter(fmt, "Source Record", record_mode=RECORD_MODE_NONE)
    for fmt in formats:
        wait_for_files(obs, fmt, 1)
    check(not unreadable, f"not readable while recording: {unreadable}")


def mp4_finished(path):
    """A Hybrid MP4 swaps the iso6 brand for obs1 when it is finalised."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags=compatible_brands", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    brands = json.loads(result.stdout or "{}").get("format", {}).get("tags", {}).get("compatible_brands", "")
    return "obs1" in brands


# Checks that can only run once OBS has exited.
AFTER_EXIT = []

RED = 0xFF0000FF  # OBS colours are ABGR
BLUE = 0xFFFF0000


def case_combined_file_has_track_per_source(obs):
    """Filters with "combined" set share one Hybrid MP4, one video and one
    audio track per source, in source-name order."""
    shared = {"path": str(obs.out / "Combined"), "record_mode": RECORD_MODE_MANUAL, "combined_file": True}
    obs.add_color_source("Combined Red", size=(320, 240), color=RED)
    obs.add_color_source("Combined Blue", size=(640, 360), color=BLUE)
    obs.add_filter("Combined Red", "Source Record", **shared)
    obs.add_filter("Combined Blue", "Source Record", **shared)
    obs.hotkey("source_record.Start")
    check(wait_until(lambda: obs.files("Combined"), 5), "no combined file appeared")
    time.sleep(3)
    obs.hotkey("source_record.Stop")
    files = wait_for_files(obs, "Combined", 1)
    check(len(files) == 1 and files[0].suffix == ".mp4", f"expected one mp4, got {[f.name for f in files]}")
    check(wait_until(lambda: mp4_finished(files[0]), 5), "the combined file was not finalised after Stop")
    streams = probe_streams(files[0])
    print("      " + "; ".join(f"{s['codec_type']} {s.get('width', '')}x{s.get('height', '')} "
                              f"{s.get('nb_read_frames')} {s.get('tags', {})}" for s in streams))
    video = [(s["width"], s["height"]) for s in streams if s["codec_type"] == "video"]
    audio = [s for s in streams if s["codec_type"] == "audio"]
    check(video == [(640, 360), (320, 240)], f"video tracks: {video}")
    check(len(audio) == 2, f"expected 2 audio tracks, got {len(audio)}")
    timing = [(float(s["start_time"]), float(s["duration"])) for s in streams if s["codec_type"] == "video"]
    starts = [t[0] for t in timing]
    ends = [t[0] + t[1] for t in timing]
    check(max(starts) - min(starts) < 0.1 and max(ends) - min(ends) < 0.1, f"video tracks out of step: {timing}")
    for name in ("Combined Red", "Combined Blue"):
        check(not obs.files(name), f"{name} also wrote its own file")


def video_starts_clean(path, index):
    """The track's first frame is a keyframe and the track decodes without errors."""
    first = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", f"v:{index}", "-read_intervals", "%+#1",
         "-show_entries", "frame=key_frame", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    frames = json.loads(first.stdout or "{}").get("frames", [])
    decode = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", f"0:v:{index}", "-f", "null", "-"],
                            capture_output=True, text=True)
    return bool(frames) and frames[0].get("key_frame") == 1 and not decode.stderr.strip()


def case_combined_file_split_all(obs):
    """Split All on a combined recording: two finished files, every track of
    both starting on a keyframe."""
    shared = {"path": str(obs.out / "Combined Split"), "record_mode": RECORD_MODE_MANUAL, "combined_file": True}
    obs.add_color_source("Split Red", size=(320, 240), color=RED)
    obs.add_color_source("Split Blue", size=(640, 360), color=BLUE)
    obs.add_filter("Split Red", "Source Record", **shared)
    obs.add_filter("Split Blue", "Source Record", **shared)
    obs.hotkey("source_record.Start")
    check(wait_until(lambda: obs.files("Combined Split"), 5), "no combined file appeared")
    time.sleep(3)
    obs.hotkey("source_record.SplitAll")
    time.sleep(3)
    obs.hotkey("source_record.Stop")
    files = wait_for_files(obs, "Combined Split", 2)
    check(len(files) == 2, f"expected 2 files after one split, got {[f.name for f in files]}")
    for f in files:
        check(wait_until(lambda: mp4_finished(f), 5), f"{f.name} was not finalised")
        video = [s for s in probe_streams(f) if s["codec_type"] == "video"]
        check(len(video) == 2, f"{f.name}: expected 2 video tracks, got {len(video)}")
        for index in range(2):
            check(video_starts_clean(f, index), f"{f.name}: video track {index} does not start on a keyframe")


def case_combined_file_finished_on_exit(obs):
    """Quitting OBS mid-recording must finish the combined file, not hang."""
    obs.add_color_source("Exit Red", color=RED)
    obs.add_color_source("Exit Blue", size=(640, 360), color=BLUE)
    for name in ("Exit Red", "Exit Blue"):
        obs.add_filter(name, "Source Record", path=str(obs.out / "Exit"), record_mode=RECORD_MODE_ALWAYS,
                       combined_file=True)
    check(wait_until(lambda: obs.files("Exit"), 5), "no combined file appeared")
    time.sleep(2)

    def finished_after_exit():
        files = obs.files("Exit")
        check(len(files) == 1 and mp4_finished(files[0]), f"not finalised at exit: {[f.name for f in files]}")
        video = [s for s in probe_streams(files[0]) if s["codec_type"] == "video"]
        check(len(video) == 2, f"expected 2 video tracks, got {len(video)}")

    AFTER_EXIT.append(("combined_file_finished_on_exit", finished_after_exit))


def case_startup_shutdown(obs):
    """Baseline: OBS with one plain source must start and exit cleanly."""
    obs.add_color_source("Idle Cam")


CASES = {name[len("case_"):]: fn for name, fn in globals().items() if name.startswith("case_")}


def ensure_display():
    socket = pathlib.Path("/tmp/.X11-unix") / f"X{X_DISPLAY[1:]}"
    if socket.exists():
        return None
    xvfb = subprocess.Popen(
        ["Xvfb", X_DISPLAY, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    check(wait_until(socket.exists, 10), "Xvfb did not start")
    return xvfb


def nix_build_plugin():
    result = subprocess.run(
        ["nix", "build", f"{REPO}#default", "--no-link", "--print-out-paths"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip().splitlines()[-1]


def main():
    # obsws-python logs a traceback for every refused connection while OBS starts.
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin", help="install prefix of the plugin under test")
    parser.add_argument("--keep", action="store_true", help="keep the work directory")
    parser.add_argument("cases", nargs="*", help=f"cases to run (default: all of {', '.join(CASES)})")
    args = parser.parse_args()

    selected = args.cases or list(CASES)
    unknown = [c for c in selected if c not in CASES]
    if unknown:
        parser.error(f"unknown cases: {unknown}")

    plugin = args.plugin or nix_build_plugin()
    xvfb = ensure_display()
    runs = REPO / "tests" / "runs"
    runs.mkdir(exist_ok=True)
    workdir = pathlib.Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=runs))
    obs = Obs(plugin, workdir)
    failures = []
    try:
        obs.start()
        for name in selected:
            try:
                started = time.monotonic()
                CASES[name](obs)
                print(f"PASS  {name} ({time.monotonic() - started:.0f} s)", flush=True)
            except Failure as e:
                failures.append(name)
                print(f"FAIL  {name}: {e}", flush=True)
            except Exception:
                failures.append(name)
                print(f"ERROR {name}:\n{traceback.format_exc()}")
    finally:
        exit_code = obs.stop() if obs.proc else None
        if xvfb:
            xvfb.terminate()
    if exit_code:
        failures.append("shutdown")
        print(f"FAIL  shutdown: OBS exited with {exit_code}")
    for name, after_exit in AFTER_EXIT:
        try:
            after_exit()
            print(f"PASS  {name} (after exit)")
        except Failure as e:
            failures.append(name)
            print(f"FAIL  {name} (after exit): {e}")
    if obs.proc and not exit_code:
        leaks = obs.leaks()
        if leaks != 0:
            failures.append("leaks")
            print(f"FAIL  leaks: OBS reported {leaks} memory leaks on exit")
        remaining = obs.remaining_sources()
        if remaining:
            failures.append("remaining sources")
            print(f"FAIL  remaining sources: {remaining} sources were never freed")
    print(f"work directory: {workdir}" if args.keep or failures else "")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
