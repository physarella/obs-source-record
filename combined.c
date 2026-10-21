#include <stdlib.h>
#include <string.h>

#include <obs-module.h>
#include <obs-frontend-api.h>
#include <util/darray.h>
#include <util/dstr.h>
#include <util/platform.h>
#include <util/threading.h>

#include "source-record.h"

#ifndef MAX_OUTPUT_VIDEO_ENCODERS
#define MAX_OUTPUT_VIDEO_ENCODERS 10
#endif

/* Members that start within this window share the first file instead of
 * restarting it once per member. */
#define SETTLE_NS 500000000ULL

static void (*set_video_encoder2)(obs_output_t *output, obs_encoder_t *encoder, size_t idx);

/* `mutex` guards the state below and is only held briefly, because every
 * filter's tick takes it. `start_mutex` is held while the output starts or
 * stops, so a filter being destroyed waits for that instead of racing it. */
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t start_mutex = PTHREAD_MUTEX_INITIALIZER;
static DARRAY(struct source_record_filter_context *) members;
static DARRAY(struct source_record_filter_context *) in_output;
static obs_output_t *output;
static bool changed;
static bool stopping;
static bool update_queued;
static uint64_t changed_at;

bool combined_available(void)
{
	return set_video_encoder2 != NULL;
}

/* The MP4 writer finishes a stop, forced or not, on the next packet it gets,
 * so a stop has only happened once the output says it is inactive. */
static bool wait_until_stopped(obs_output_t *target, uint64_t timeout_ms)
{
	const uint64_t deadline = os_gettime_ns() + timeout_ms * 1000000ULL;
	while (obs_output_active(target) && os_gettime_ns() < deadline)
		os_sleep_ms(10);
	return !obs_output_active(target);
}

/* Must run before the encoders shut down: the stop needs their packets. */
static void finish_on_exit(enum obs_frontend_event event, void *data)
{
	UNUSED_PARAMETER(data);
	if (event != OBS_FRONTEND_EVENT_EXIT)
		return;

	pthread_mutex_lock(&start_mutex);
	pthread_mutex_lock(&mutex);
	obs_output_t *current = output;
	output = NULL;
	in_output.num = 0;
	members.num = 0;
	changed = false;
	pthread_mutex_unlock(&mutex);

	if (current) {
		obs_output_stop(current);
		if (!wait_until_stopped(current, 3000)) {
			obs_output_force_stop(current);
			wait_until_stopped(current, 2000);
		}
		if (obs_output_active(current))
			blog(LOG_WARNING, "[Source Record] combined file still active at exit, leaving it");
		else
			obs_output_release(current);
	}
	pthread_mutex_unlock(&start_mutex);
}

void combined_load(void *obs_handle)
{
	set_video_encoder2 = (void (*)(obs_output_t *, obs_encoder_t *, size_t))os_dlsym(obs_handle,
											     "obs_output_set_video_encoder2");
	obs_frontend_add_event_callback(finish_on_exit, NULL);
}

static void mark_changed(void)
{
	changed = true;
	changed_at = os_gettime_ns();
}

void combined_join(struct source_record_filter_context *context)
{
	pthread_mutex_lock(&mutex);
	if (!context->in_combined) {
		da_push_back(members, &context);
		context->in_combined = true;
		mark_changed();
	}
	pthread_mutex_unlock(&mutex);
}

void combined_leave(struct source_record_filter_context *context, bool now)
{
	if (now)
		pthread_mutex_lock(&start_mutex);
	pthread_mutex_lock(&mutex);
	if (context->in_combined) {
		da_erase_item(members, &context);
		context->in_combined = false;
		mark_changed();
	}
	/* Force-stopping an output that has already stopped resets its stopping
	 * event with nothing left to signal it, and releasing it then hangs. */
	const bool uses_context = output && da_find(in_output, &context, 0) != DARRAY_INVALID;
	obs_output_t *to_stop = now && uses_context && obs_output_active(output) ? obs_output_get_ref(output) : NULL;
	if (to_stop)
		in_output.num = 0;
	pthread_mutex_unlock(&mutex);
	if (to_stop) {
		obs_output_force_stop(to_stop);
		if (!wait_until_stopped(to_stop, 3000))
			blog(LOG_WARNING, "[Source Record] combined file did not stop in time");
		obs_output_release(to_stop);
	}
	if (now)
		pthread_mutex_unlock(&start_mutex);
}

obs_output_t *combined_output(struct source_record_filter_context *context)
{
	pthread_mutex_lock(&mutex);
	obs_output_t *result = NULL;
	if (output && da_find(in_output, &context, 0) != DARRAY_INVALID)
		result = output;
	pthread_mutex_unlock(&mutex);
	return result;
}

static bool member_ready(struct source_record_filter_context *context)
{
	return context->video_output && context->encoder && obs_encoder_video(context->encoder) == context->video_output;
}

static int compare_by_source_name(const void *a, const void *b)
{
	struct source_record_filter_context *const *ca = a;
	struct source_record_filter_context *const *cb = b;
	const char *na = obs_source_get_name(obs_filter_get_parent((*ca)->source));
	const char *nb = obs_source_get_name(obs_filter_get_parent((*cb)->source));
	return strcmp(na ? na : "", nb ? nb : "");
}

static obs_output_t *create_output(struct source_record_filter_context *first)
{
	obs_data_t *settings = obs_source_get_settings(first->source);
	char *filename = os_generate_formatted_filename("mp4", true, obs_data_get_string(settings, "filename_formatting"));
	struct dstr path;
	dstr_init(&path);
	dstr_printf(&path, "%s/%s", obs_data_get_string(settings, "path"), filename);
	bfree(filename);
	char *slash = strrchr(path.array, '/');
	*slash = 0;
	os_mkdirs(path.array);
	*slash = '/';
	claim_unique_path(&path);

	obs_data_t *s = obs_data_create();
	obs_data_set_string(s, "path", path.array);
	obs_data_set_string(s, "directory", obs_data_get_string(settings, "path"));
	obs_data_set_string(s, "format", obs_data_get_string(settings, "filename_formatting"));
	obs_data_set_string(s, "extension", "mp4");
	obs_data_set_bool(s, "allow_spaces", true);
	obs_data_set_bool(s, "split_file", true);
	const bool auto_split = obs_data_get_bool(settings, "split_file");
	obs_data_set_int(s, "max_size_mb", auto_split ? obs_data_get_int(settings, "max_size_mb") : 0);
	obs_data_set_int(s, "max_time_sec", auto_split ? obs_data_get_int(settings, "max_time_sec") : 0);
	obs_output_t *result = obs_output_create("mp4_output", "Source Record (combined)", s, NULL);
	obs_data_release(s);
	obs_data_release(settings);
	dstr_free(&path);
	return result;
}

/* The MP4 writer names each track after its encoder, so naming the encoders
 * after their sources is what labels the tracks in the file. */
static bool add_member_tracks(obs_output_t *target, struct source_record_filter_context *context, size_t *video_idx,
			      size_t *audio_idx)
{
	const char *name = obs_source_get_name(obs_filter_get_parent(context->source));
	if (*video_idx >= MAX_OUTPUT_VIDEO_ENCODERS) {
		blog(LOG_WARNING, "[Source Record] combined file is full, '%s' is left out", name);
		return false;
	}
	obs_encoder_set_name(context->encoder, name);
	set_video_encoder2(target, context->encoder, (*video_idx)++);

	if (context->audioEncoder[0] && *audio_idx < MAX_OUTPUT_AUDIO_ENCODERS) {
		obs_encoder_set_name(context->audioEncoder[0], name);
		obs_output_set_audio_encoder(target, context->audioEncoder[0], (*audio_idx)++);
	}
	return true;
}

/* Called with start_mutex held; members cannot leave with `now` meanwhile. */
static obs_output_t *start_output(struct source_record_filter_context **sorted, size_t count)
{
	obs_output_t *started = create_output(sorted[0]);
	size_t video_idx = 0;
	size_t audio_idx = 0;
	DARRAY(struct source_record_filter_context *) used = {0};
	for (size_t i = 0; i < count; i++) {
		if (add_member_tracks(started, sorted[i], &video_idx, &audio_idx))
			da_push_back(used, &sorted[i]);
	}

	if (!obs_output_start(started)) {
		blog(LOG_WARNING, "[Source Record] combined file failed to start: %s", obs_output_get_last_error(started));
		obs_output_release(started);
		da_free(used);
		return NULL;
	}
	pthread_mutex_lock(&mutex);
	da_move(in_output, used);
	pthread_mutex_unlock(&mutex);
	return started;
}

/* Restarts the shared output whenever the set of members changes: an active
 * output cannot take new encoders. */
static void update_output(void *unused)
{
	UNUSED_PARAMETER(unused);
	pthread_mutex_lock(&start_mutex);
	pthread_mutex_lock(&mutex);
	update_queued = false;

	obs_output_t *current = output;
	if (current && obs_output_active(current)) {
		const bool stop = changed && !stopping;
		stopping = stopping || stop;
		pthread_mutex_unlock(&mutex);
		if (stop)
			obs_output_stop(current);
		pthread_mutex_unlock(&start_mutex);
		return;
	}

	output = NULL;
	in_output.num = 0;
	stopping = false;

	DARRAY(struct source_record_filter_context *) sorted = {0};
	if (changed && !members.num) {
		changed = false;
	} else if (changed && os_gettime_ns() - changed_at >= SETTLE_NS) {
		bool all_ready = true;
		for (size_t i = 0; i < members.num; i++)
			all_ready = all_ready && member_ready(members.array[i]);
		if (all_ready) {
			changed = false;
			da_copy(sorted, members);
			qsort(sorted.array, sorted.num, sizeof(*sorted.array), compare_by_source_name);
		}
	}
	pthread_mutex_unlock(&mutex);

	if (current)
		obs_output_release(current);
	if (sorted.num) {
		obs_output_t *started = start_output(sorted.array, sorted.num);
		pthread_mutex_lock(&mutex);
		output = started;
		pthread_mutex_unlock(&mutex);
	}
	da_free(sorted);
	pthread_mutex_unlock(&start_mutex);
}

void combined_poll(void)
{
	pthread_mutex_lock(&mutex);
	const bool needs_update = (changed || (output && !obs_output_active(output))) && !update_queued;
	if (needs_update)
		update_queued = true;
	pthread_mutex_unlock(&mutex);
	if (needs_update)
		obs_queue_task(OBS_TASK_UI, update_output, NULL, false);
}

void combined_unload(void)
{
	obs_frontend_remove_event_callback(finish_on_exit, NULL);
	pthread_mutex_lock(&start_mutex);
	pthread_mutex_lock(&mutex);
	/* Releasing an active output here would wait forever for a stop that
	 * needs encoders which are already gone. */
	if (output && !obs_output_active(output))
		obs_output_release(output);
	output = NULL;
	da_free(members);
	da_free(in_output);
	pthread_mutex_unlock(&mutex);
	pthread_mutex_unlock(&start_mutex);
}
