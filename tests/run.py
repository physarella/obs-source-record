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
X_DISPLAY = ":97"
CANVAS = (320, 240)

RECORD_MODE_NONE = 0
RECORD_MODE_ALWAYS = 1
RECORD_MODE_STREAMING = 2
RECORD_MODE_RECORDING = 3

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
                self.proc.kill()
                self.proc.wait()
                raise Failure("OBS hung on shutdown")
        self.log.close()
        return self.proc.returncode

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

    def add_color_source(self, name, size=CANVAS, visible=True):
        self.request(
            "CreateInput",
            {
                "sceneName": "Scene",
                "inputName": name,
                "inputKind": "color_source_v3",
                "inputSettings": {"color": 0xFF3366CC, "width": size[0], "height": size[1]},
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
                CASES[name](obs)
                print(f"PASS  {name}")
            except Failure as e:
                failures.append(name)
                print(f"FAIL  {name}: {e}")
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
    elif obs.proc:
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
