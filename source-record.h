#pragma once

#include <stdbool.h>

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
