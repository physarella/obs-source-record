"""Shows and sets which audio track of a movie file each sound strip plays.

Blender keeps the track in bSound.stream_index but shows it nowhere, and an
edit can reset it to the first track without notice: seen in 5.2.2, where a
strip meant for a recording's second track silently played the first.
"""

import ctypes
import json
import os
import struct
import subprocess
from functools import lru_cache

import bpy

# bSound in Blender 5.2, after filepath[1024]: three pointers, a double, six
# floats, then audio_channels and samplerate (ints), flags and stream_index
# (shorts). DNA structs have no hidden padding, so these offsets are exact.
CHANNELS_AFTER_PATH = 1080
STREAM_INDEX_AFTER_PATH = 1090
STRUCT_BYTES_TO_SCAN = 4096


class LayoutError(Exception):
    """The sound's memory does not look like the bSound layout above."""


def stream_index_address(sound):
    """Address of the sound's stream_index. Raises LayoutError unless the two
    neighbouring fields Blender does expose read back the same values."""
    base = sound.as_pointer()
    raw = ctypes.string_at(base, STRUCT_BYTES_TO_SCAN)
    path_at = raw.find(sound.filepath.encode() + b"\0")
    if path_at < 0:
        raise LayoutError("Sound file path not found")
    channels, rate = struct.unpack_from("<ii", raw, path_at + CHANNELS_AFTER_PATH)
    expected_channels = sound.bl_rna.properties["channels"].enum_items[sound.channels].value
    if rate == 0 or (channels, rate) != (expected_channels, sound.samplerate):
        raise LayoutError("Unsupported Blender version or sound not loaded yet")
    return base + path_at + STREAM_INDEX_AFTER_PATH


def get_stream_index(sound):
    return ctypes.c_short.from_address(stream_index_address(sound)).value


def set_stream_index(sound, index):
    ctypes.c_short.from_address(stream_index_address(sound)).value = index
    # Re-assigning the path makes Blender reload the sound with the new track.
    sound.filepath = sound.filepath


@lru_cache(maxsize=64)
def _probe_tracks(path, mtime):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:stream_tags=name,title",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30,
        )
        streams = json.loads(result.stdout).get("streams", [])
    except (OSError, subprocess.SubprocessError, ValueError):
        streams = []
    tracks = {"audio": [], "video": []}
    for stream in streams:
        kind = stream.get("codec_type")
        if kind in tracks:
            tags = stream.get("tags", {})
            tracks[kind].append(tags.get("name") or tags.get("title") or "")
    return tracks


def track_names(filepath, kind):
    """Names of the file's audio or video tracks, in stream order. Tracks
    without a name, and every track when ffprobe is missing, get a number."""
    path = bpy.path.abspath(filepath)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    names = _probe_tracks(path, mtime)[kind]
    return [name or f"{kind.title()} {i + 1}" for i, name in enumerate(names)]


def is_multitrack_sound(strip):
    return strip.type == 'SOUND' and strip.sound and len(track_names(strip.sound.filepath, "audio")) > 1


def sound_strips(scene):
    if not scene or not scene.sequence_editor:
        return []
    return [s for s in scene.sequence_editor.strips_all if is_multitrack_sound(s)]


def overlapping(a, b):
    return a.frame_final_start < b.frame_final_end and b.frame_final_start < a.frame_final_end


def same_track_conflicts(scene):
    """Pairs of sound strips that play the same track of the same file at the
    same time: what a reset track looks like."""
    keyed = {}
    for strip in sound_strips(scene):
        try:
            key = (bpy.path.abspath(strip.sound.filepath), get_stream_index(strip.sound))
        except LayoutError:
            continue
        keyed.setdefault(key, []).append(strip)
    pairs = []
    for strips in keyed.values():
        for i, a in enumerate(strips):
            pairs += [(a, b) for b in strips[i + 1:] if overlapping(a, b)]
    return pairs


# Blender crashes if the items of a dynamic enum are garbage collected while
# its menu is open, so they are kept here.
_item_lists = {}


def _audio_track_items(self, context):
    names = track_names(self.sound.filepath, "audio") if self.sound else []
    items = [(str(i), f"{i + 1}: {name}", f"Play audio track {i + 1} of the file", i) for i, name in enumerate(names)]
    _item_lists[self.as_pointer()] = items
    return items


def _get_audio_track(self):
    try:
        return get_stream_index(self.sound)
    except LayoutError:
        return 0


def _set_audio_track(self, value):
    try:
        set_stream_index(self.sound, value)
    except LayoutError as error:
        print(f"Combined Recording Tracks: {error}")


class SEQUENCER_OT_separate_track_sound(bpy.types.Operator):
    bl_idname = "sequencer.separate_track_sound"
    bl_label = "Separate"
    bl_description = "Give this strip its own copy of the sound, so its track can change without changing the others"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        strip = context.active_strip
        return strip is not None and strip.type == 'SOUND' and strip.sound is not None

    def execute(self, context):
        context.active_strip.sound = context.active_strip.sound.copy()
        return {'FINISHED'}


class SEQUENCER_OT_name_strips_after_tracks(bpy.types.Operator):
    bl_idname = "sequencer.name_strips_after_tracks"
    bl_label = "Name Strips After Tracks"
    bl_description = "Rename every strip of this file after the track it shows or plays"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        strip = context.active_strip
        return strip is not None and strip.type in {'MOVIE', 'SOUND'}

    def execute(self, context):
        active = context.active_strip
        path = bpy.path.abspath(active.sound.filepath if active.type == 'SOUND' else active.filepath)
        video_names = track_names(path, "video")
        audio_names = track_names(path, "audio")
        renamed = 0
        for strip in context.sequencer_scene.sequence_editor.strips_all:
            if strip.type == 'MOVIE' and bpy.path.abspath(strip.filepath) == path:
                if strip.stream_index < len(video_names):
                    strip.name = f"{video_names[strip.stream_index]} video"
                    renamed += 1
            elif strip.type == 'SOUND' and strip.sound and bpy.path.abspath(strip.sound.filepath) == path:
                try:
                    index = get_stream_index(strip.sound)
                except LayoutError:
                    continue
                if index < len(audio_names):
                    strip.name = f"{audio_names[index]} audio"
                    renamed += 1
        self.report({'INFO'}, f"Renamed {renamed} strips")
        return {'FINISHED'}


class STRIP_PT_audio_track(bpy.types.Panel):
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "strip"
    bl_label = "Audio Track"
    bl_parent_id = "STRIP_PT_adjust_sound"

    @classmethod
    def poll(cls, context):
        strip = context.active_strip
        return strip is not None and is_multitrack_sound(strip)

    def draw(self, context):
        layout = self.layout
        strip = context.active_strip
        try:
            stream_index_address(strip.sound)
        except LayoutError as error:
            layout.label(text=str(error), icon='ERROR')
            return

        layout.prop(strip, "audio_track", text="Track")

        sharing = [s for s in context.sequencer_scene.sequence_editor.strips_all
                   if s.type == 'SOUND' and s.sound == strip.sound and s != strip]
        if sharing:
            row = layout.row()
            row.label(text=f"Shared with {len(sharing)} other strip(s)", icon='LINKED')
            row.operator(SEQUENCER_OT_separate_track_sound.bl_idname)

        for a, b in same_track_conflicts(context.sequencer_scene):
            if strip in (a, b):
                other = b if strip == a else a
                layout.label(text=f"Same track as '{other.name}' at the same time", icon='ERROR')

        layout.operator(SEQUENCER_OT_name_strips_after_tracks.bl_idname, icon='SORTALPHA')


def _report_conflicts():
    pairs = []
    for scene in bpy.data.scenes:
        pairs += same_track_conflicts(scene)
    if not pairs or not bpy.context.window_manager.windows:
        return None

    def draw(menu, _context):
        for a, b in pairs:
            menu.layout.label(text=f"'{a.name}' and '{b.name}' play the same audio track at the same time")
        menu.layout.label(text="Check Strip properties > Audio Track")

    window = bpy.context.window_manager.windows[0]
    with bpy.context.temp_override(window=window):
        bpy.context.window_manager.popup_menu(draw, title="Combined recording audio", icon='ERROR')
    return None


@bpy.app.handlers.persistent
def _check_after_load(_filepath):
    bpy.app.timers.register(_report_conflicts, first_interval=1.0)


classes = (SEQUENCER_OT_separate_track_sound, SEQUENCER_OT_name_strips_after_tracks, STRIP_PT_audio_track)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.SoundStrip.audio_track = bpy.props.EnumProperty(
        name="Audio Track",
        description="Which audio track of the movie file this strip plays",
        items=_audio_track_items,
        get=_get_audio_track,
        set=_set_audio_track,
    )
    bpy.app.handlers.load_post.append(_check_after_load)


def unregister():
    bpy.app.handlers.load_post.remove(_check_after_load)
    del bpy.types.SoundStrip.audio_track
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
