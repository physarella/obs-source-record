#pragma once

#include <obs-module.h>

struct source_record_filter_context {
	obs_source_t *source;
	video_t *video_output;
	audio_t *audio_output;
	bool output_active;
	uint32_t width;
	uint32_t height;
	uint64_t last_frame_time_ns;
	obs_view_t *view;
	bool starting_file_output;
	bool starting_stream_output;
	bool starting_replay_output;
	bool restart;
	obs_output_t *fileOutput;
	obs_output_t *streamOutput;
	obs_output_t *replayOutput;
	obs_encoder_t *encoder;
	obs_encoder_t *audioEncoder[MAX_AUDIO_MIXES];
	obs_service_t *service;
	bool record;
	bool stream;
	bool replayBuffer;
	obs_hotkey_pair_id enableHotkey;
	obs_hotkey_pair_id pauseHotkeys;
	obs_hotkey_id splitHotkey;
	obs_hotkey_id chapterHotkey;
	obs_hotkey_id saveReplayHotkey;
	int audio_track;
	obs_weak_source_t *audio_source;
	bool closing;
	bool exiting;
	long long replay_buffer_duration;
	struct vec4 backgroundColor;
	bool remove_after_record;
	long long record_max_seconds;
	int last_frontend_event;
	bool combined;
	bool in_combined;
};

/* Every filter with "combined" set records into one Hybrid MP4, one video
 * and one audio track per source (combined.c). */
void combined_join(struct source_record_filter_context *context);
/* `now` force-stops the shared output if it uses this filter's encoders, for
 * when the filter's video output is about to be freed. */
void combined_leave(struct source_record_filter_context *context, bool now);
obs_output_t *combined_output(struct source_record_filter_context *context);
void combined_poll(void);
/* False on OBS before 30.2, which cannot put several videos in one output. */
bool combined_available(void);
void combined_load(void *obs_handle);
void combined_unload(void);

struct dstr;
/* Makes the path unique with a " (2)"-style suffix and creates the file. */
void claim_unique_path(struct dstr *path);

/* The global switch behind the Start, Stop and Toggle Source Recording
 * hotkeys and the Tools menu entry (controls.c). */
bool source_record_manual_active(void);
/* True after Stop until the next Start, or until streaming, recording or the
 * virtual camera starts: filters whose record mode follows one of those stay
 * stopped meanwhile. */
bool source_record_held(void);
void source_record_controls_load(void);
void source_record_controls_unload(void);

/* Implemented in source-record.c, over every Source Record filter. */
void source_record_update_all(void);
void source_record_split_all(void);
bool source_record_any_recording(void);
