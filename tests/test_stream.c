/* Virtual-time end-to-end loop test: no hardware or sleeping. */
#define main bosto_driver_main
#include "../src/main.c"
#undef main
#include <assert.h>

static double virtual_time, mock_encode = .004, mock_usb = .010;
static double previous_frame, minimum_period;
static unsigned sent, poll_calls, roi_frames;
static bool have_mode, idle;

int __wrap_clock_gettime(clockid_t clock, struct timespec *ts)
{
    (void)clock;
    ts->tv_sec = (time_t)virtual_time;
    ts->tv_nsec = (long)((virtual_time - (double)ts->tv_sec) * 1e9);
    return 0;
}
int __wrap_poll(struct pollfd *fd, nfds_t count, int timeout)
{
    assert(count == 1 && timeout >= 0 && timeout <= 1000);
    ++poll_calls;
    assert(poll_calls < 10000);
    if (!have_mode) { fd->revents = POLLIN; return 1; }
    virtual_time += (double)timeout / 1000.0;
    fd->revents = 0;
    return 0;
}
evdi_handle __wrap_evdi_open_attached_to_fixed(const char *parent, size_t length)
{
    assert(!parent && !length); return (evdi_handle)(uintptr_t)1;
}
void __wrap_evdi_enable_cursor_events(evdi_handle h, bool enabled) { assert(h && !enabled); }
void __wrap_evdi_connect2(evdi_handle h, const unsigned char *edid, unsigned int length,
                          uint32_t area, uint32_t rate)
{ assert(h && edid && length == 128 && area == 1280*800 && rate == 1280*800*60); }
evdi_selectable __wrap_evdi_get_event_ready(evdi_handle h) { assert(h); return 99; }
void __wrap_evdi_handle_events(evdi_handle h, struct evdi_event_context *events)
{
    assert(h && !have_mode);
    have_mode = true;
    struct evdi_mode mode = {1280,800,60,32,DRM_FORMAT_ARGB8888};
    events->mode_changed_handler(mode, events->user_data);
}
void __wrap_evdi_register_buffer(evdi_handle h, struct evdi_buffer b) { assert(h && b.buffer); }
void __wrap_evdi_unregister_buffer(evdi_handle h, int id) { assert(h && id); }
bool __wrap_evdi_request_update(evdi_handle h, int id) { assert(h && id); return !idle; }
void __wrap_evdi_grab_pixels(evdi_handle h, struct evdi_rect *rects, int *count)
{
    assert(h && *count == 16);
    rects[0] = (struct evdi_rect){17,9,18,10}; *count = 1;
    virtual_time += .001;
}
void __wrap_evdi_disconnect(evdi_handle h) { assert(h); }
void __wrap_evdi_close(evdi_handle h) { assert(h); }
int __wrap_bosto_encode_jpeg(const uint8_t *pixels, unsigned w, unsigned h,
    unsigned stride, int quality, uint8_t **jpeg, size_t *length,
    char *error, size_t error_size)
{
    (void)error; (void)error_size;
    assert(pixels && w && h && stride == 1280*4 && quality == 90);
    *jpeg = malloc(4); assert(*jpeg);
    memcpy(*jpeg, "\xff\xd8\xff\xd9", 4); *length = 4;
    virtual_time += mock_encode; return 0;
}
int __wrap_bosto_extract_dqt(const uint8_t *jpeg, size_t length, uint8_t dqt[138])
{ assert(jpeg && length == 4); memset(dqt,0,138); return 0; }
int __wrap_racer_usb_set_quant_table(struct racer_usb *usb, const uint8_t dqt[138])
{ assert(usb && dqt); return 0; }
int __wrap_racer_usb_set_resolution(struct racer_usb *usb, uint16_t w, uint16_t h)
{ assert(usb && w == 1280 && h == 800); return 0; }
int __wrap_racer_usb_send_jpeg(struct racer_usb *usb, const uint8_t *jpeg,
    size_t length, uint32_t w, uint32_t h, uint32_t x, uint32_t y)
{
    assert(usb && jpeg && length == 4);
    bool full = w == 1280 && h == 800 && x == 0 && y == 0;
    if (!full) { assert(w == 16 && h == 8 && x == 16 && y == 8); ++roi_frames; }
    if (!sent) assert(full);
    else assert(virtual_time - previous_frame >= minimum_period - .000001);
    previous_frame = virtual_time;
    ++sent; virtual_time += mock_usb; return 0;
}
static void scenario(int fps, double encode_time, double usb_time, bool no_changes, bool damage)
{
    virtual_time = previous_frame = 0; mock_encode = encode_time; mock_usb = usb_time;
    sent = poll_calls = roi_frames = 0; have_mode = false; idle = no_changes;
    minimum_period = 1.0 / fps;
    struct racer_usb usb = {0}; uint8_t edid[128] = {0};
    assert(run_stream(&usb, edid, sizeof(edid), 1280,800,fps,90,3,true,damage) == 0);
    if (no_changes) assert(sent == 0 && poll_calls <= 5);
    else if (encode_time < .01) {
        assert(sent >= (unsigned)(fps * 2.8) && sent <= (unsigned)(fps * 3 + 1));
        if (damage) assert(roi_frames > 1 && roi_frames < sent);
    } else assert(sent >= 48 && sent <= 50); /* 61ms work; no added33ms sleep */
    printf("Virtual stream: target%d sent%u in%.3fs, polls%u, ROI%u\n",
           fps, sent, virtual_time, poll_calls, roi_frames);
}
int main(void)
{
    scenario(30,.004,.010,false,false);
    scenario(60,.004,.010,false,false);
    scenario(30,.040,.020,false,false);
    scenario(30,.004,.010,true,false);
    scenario(30,.004,.010,false,true);
    return 0;
}
