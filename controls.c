#include <obs-module.h>
#include <obs-frontend-api.h>
#include <util/config-file.h>

#include "source-record.h"

#define HOTKEY_START "source_record.Start"
#define HOTKEY_STOP "source_record.Stop"
#define HOTKEY_TOGGLE "source_record.Toggle"
#define HOTKEY_SPLIT_ALL "source_record.SplitAll"

static bool manual_active;
static bool held;
static obs_hotkey_pair_id start_stop_hotkeys = OBS_INVALID_HOTKEY_PAIR_ID;
static obs_hotkey_id toggle_hotkey = OBS_INVALID_HOTKEY_ID;
static obs_hotkey_id split_all_hotkey = OBS_INVALID_HOTKEY_ID;

bool source_record_manual_active(void)
{
	return manual_active;
}

bool source_record_held(void)
{
	return held;
}

static void set_recording(bool active)
{
	manual_active = active;
	held = !active;
	source_record_update_all();
}

static bool start_hotkey(void *data, obs_hotkey_pair_id id, obs_hotkey_t *hotkey, bool pressed)
{
	UNUSED_PARAMETER(data);
	UNUSED_PARAMETER(id);
	UNUSED_PARAMETER(hotkey);
	if (!pressed || (manual_active && !held))
		return false;
	set_recording(true);
	return true;
}

static bool stop_hotkey(void *data, obs_hotkey_pair_id id, obs_hotkey_t *hotkey, bool pressed)
{
	UNUSED_PARAMETER(data);
	UNUSED_PARAMETER(id);
	UNUSED_PARAMETER(hotkey);
	if (!pressed || (!manual_active && held))
		return false;
	set_recording(false);
	return true;
}

static void toggle(void)
{
	set_recording(!source_record_any_recording());
}

static void toggle_hotkey_pressed(void *data, obs_hotkey_id id, obs_hotkey_t *hotkey, bool pressed)
{
	UNUSED_PARAMETER(data);
	UNUSED_PARAMETER(id);
	UNUSED_PARAMETER(hotkey);
	if (pressed)
		toggle();
}

static void split_all_hotkey_pressed(void *data, obs_hotkey_id id, obs_hotkey_t *hotkey, bool pressed)
{
	UNUSED_PARAMETER(data);
	UNUSED_PARAMETER(id);
	UNUSED_PARAMETER(hotkey);
	if (pressed)
		source_record_split_all();
}

static void toggle_menu_clicked(void *data)
{
	UNUSED_PARAMETER(data);
	toggle();
}

/* OBS saves the bindings of every frontend hotkey to the profile's [Hotkeys]
 * section, but only loads the ones it registered itself. */
static obs_data_array_t *saved_bindings(const char *name)
{
	const char *json = config_get_string(obs_frontend_get_profile_config(), "Hotkeys", name);
	if (!json)
		return NULL;
	obs_data_t *data = obs_data_create_from_json(json);
	if (!data)
		return NULL;
	obs_data_array_t *bindings = obs_data_get_array(data, "bindings");
	obs_data_release(data);
	return bindings;
}

static void load_bindings(void)
{
	obs_data_array_t *start = saved_bindings(HOTKEY_START);
	obs_data_array_t *stop = saved_bindings(HOTKEY_STOP);
	obs_hotkey_pair_load(start_stop_hotkeys, start, stop);
	obs_data_array_release(start);
	obs_data_array_release(stop);

	obs_data_array_t *bindings = saved_bindings(HOTKEY_TOGGLE);
	obs_hotkey_load(toggle_hotkey, bindings);
	obs_data_array_release(bindings);

	bindings = saved_bindings(HOTKEY_SPLIT_ALL);
	obs_hotkey_load(split_all_hotkey, bindings);
	obs_data_array_release(bindings);
}

static void frontend_event(enum obs_frontend_event event, void *data)
{
	UNUSED_PARAMETER(data);
	switch (event) {
	case OBS_FRONTEND_EVENT_FINISHED_LOADING:
	case OBS_FRONTEND_EVENT_PROFILE_CHANGED:
		load_bindings();
		break;
	case OBS_FRONTEND_EVENT_STREAMING_STARTING:
	case OBS_FRONTEND_EVENT_RECORDING_STARTING:
	case OBS_FRONTEND_EVENT_VIRTUALCAM_STARTED:
		if (held) {
			held = false;
			source_record_update_all();
		}
		break;
	default:
		break;
	}
}

void source_record_controls_load(void)
{
	start_stop_hotkeys = obs_hotkey_pair_register_frontend(HOTKEY_START, obs_module_text("StartSourceRecording"),
							       HOTKEY_STOP, obs_module_text("StopSourceRecording"),
							       start_hotkey, stop_hotkey, NULL, NULL);
	toggle_hotkey = obs_hotkey_register_frontend(HOTKEY_TOGGLE, obs_module_text("ToggleSourceRecording"),
						     toggle_hotkey_pressed, NULL);
	split_all_hotkey = obs_hotkey_register_frontend(HOTKEY_SPLIT_ALL, obs_module_text("SplitAllSourceRecordings"),
							split_all_hotkey_pressed, NULL);
	obs_frontend_add_tools_menu_item(obs_module_text("ToggleSourceRecording"), toggle_menu_clicked, NULL);
	obs_frontend_add_event_callback(frontend_event, NULL);
}

void source_record_controls_unload(void)
{
	obs_frontend_remove_event_callback(frontend_event, NULL);
	obs_hotkey_pair_unregister(start_stop_hotkeys);
	obs_hotkey_unregister(toggle_hotkey);
	obs_hotkey_unregister(split_all_hotkey);
}
