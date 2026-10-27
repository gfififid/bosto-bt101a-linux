/* SPDX-License-Identifier: MIT */
#define _POSIX_C_SOURCE 200809L
#include "racer_usb.h"
#include "jpeg_frame.h"
#include "frame_region.h"
#include "evdi_lib.h"
#include <drm_fourcc.h>
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static volatile sig_atomic_t stopping;
static void stop_handler(int signal_number) { (void)signal_number; stopping = 1; }
static double now_seconds(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}
static void usage(const char *name)
{
    printf("Experimental Bosto BT-101A / RacerTech 8808 Linux display driver\n"
           "Usage: %s ACTION [--fps 60] [--quality 90] [--seconds N] [--stats]\n\n"
           "  --probe                  Read status and EDID; no display writes\n"
           "  --pattern                Send one 1280x800 color-bar test image\n"
           "  --run                    Expose a monitor through a loaded EVDI module\n"
           "  --offline-pattern FILE   Save a JPEG test image without USB access\n"
           "  --help                   Show this help without USB access\n\n"
           "  --stats                  Print measured refresh/encoding/USB timing every 2s\n"
           "  --damage                 Experimental partial updates for small changes\n\n"
           "USB modes normally require sudo. Only USB 34c7:2700 interface 0 is used.\n"
           "Stop --run with Ctrl+C. Pen and touch interfaces are never claimed.\n", name);
}

static int encode(const uint8_t *pixels, unsigned w, unsigned h, int quality,
                  uint8_t **jpeg, size_t *length, uint8_t dqt[RACER_DQT_SIZE])
{
    char error[256];
    if (bosto_encode_jpeg(pixels, w, h, w * 4, quality, jpeg, length, error, sizeof(error))) {
        fprintf(stderr, "JPEG: %s\n", error); return -1;
    }
    if (dqt && bosto_extract_dqt(*jpeg, *length, dqt)) {
        fprintf(stderr, "JPEG: required 8-bit quantization tables missing\n");
        free(*jpeg); *jpeg = NULL; return -1;
    }
    return 0;
}

static int read_edid(struct racer_usb *usb, uint8_t edid[512], size_t *length,
                     unsigned *width, unsigned *height)
{
    if (racer_usb_read_edid(usb, edid, 512, length)) return -1;
    const uint8_t signature[] = {0,255,255,255,255,255,255,0};
    if (*length < 128 || memcmp(edid, signature, 8)) {
        fprintf(stderr, "Invalid EDID signature\n"); return -1;
    }
    for (size_t offset = 0; offset < *length; offset += 128) {
        unsigned sum = 0;
        for (size_t i = 0; i < 128; ++i) sum += edid[offset + i];
        if (sum & 255) { fprintf(stderr, "Invalid EDID checksum (block %zu)\n", offset / 128); return -1; }
    }
    if (!edid[54] && !edid[55]) { fprintf(stderr, "EDID has no preferred detailed timing\n"); return -1; }
    *width = edid[56] | ((unsigned)(edid[58] & 0xf0) << 4);
    *height = edid[59] | ((unsigned)(edid[61] & 0xf0) << 4);
    if (!*width || !*height || *width > 4096 || *height > 4096) return -1;
    printf("EDID: %zu bytes; preferred mode %ux%u\n", *length, *width, *height);
    return 0;
}

struct stream {
    struct racer_usb *usb;
    evdi_handle evdi;
    struct evdi_buffer buffer;
    unsigned native_width, native_height;
    int quality;
    bool pending, ready, failed, force_full;
    unsigned frames;
    struct {
        double start, grab, encode, usb;
        uint64_t bytes;
        unsigned frames, partial;
    } stats;
};

static void report_stats(struct stream *s, double now, int fps)
{
    double elapsed = now - s->stats.start;
    if (elapsed < 2.0) return;
    if (!s->stats.frames) {
        fprintf(stderr, "Stats: idle, 0 changed frames; %s (target %d fps)\n",
                !s->buffer.buffer ? "waiting for a display mode" :
                s->ready ? "waiting for frame deadline" : "waiting for EVDI changes", fps);
    } else {
        double divisor = (double)s->stats.frames;
        fprintf(stderr,
                "Stats: %.1f updates/s, %.1f KiB/s JPEG; grab %.2f ms, encode %.2f ms, "
                "USB %.2f ms/update; %u/%u partial (target %d fps)\n",
                divisor / elapsed, (double)s->stats.bytes / elapsed / 1024.0,
                s->stats.grab * 1000.0 / divisor,
                s->stats.encode * 1000.0 / divisor,
                s->stats.usb * 1000.0 / divisor,
                s->stats.partial, s->stats.frames, fps);
    }
    memset(&s->stats, 0, sizeof(s->stats));
    s->stats.start = now;
}

static void mode_changed(struct evdi_mode mode, void *data)
{
    struct stream *s = data;
    if (s->buffer.buffer) {
        evdi_unregister_buffer(s->evdi, s->buffer.id);
        free(s->buffer.buffer); s->buffer.buffer = NULL;
    }
    s->pending = s->ready = false;
    s->force_full = true;
    if (!mode.width || !mode.height) return;
    printf("Display mode: %dx%d at %d Hz, %d bpp, format 0x%08x\n",
           mode.width, mode.height, mode.refresh_rate, mode.bits_per_pixel, mode.pixel_format);
    if (mode.width < 16 || mode.height < 8 ||
        (unsigned)mode.width > s->native_width || (unsigned)mode.height > s->native_height ||
        mode.width % 16 || mode.height % 8 || mode.bits_per_pixel != 32 ||
        (mode.pixel_format != DRM_FORMAT_XRGB8888 && mode.pixel_format != DRM_FORMAT_ARGB8888)) {
        fprintf(stderr, "Unsupported mode: use the panel's native mode in display settings\n");
        s->failed = true; return;
    }
    s->buffer.width = mode.width; s->buffer.height = mode.height;
    s->buffer.stride = mode.width * 4;
    ++s->buffer.id;
    s->buffer.buffer = calloc((size_t)mode.width * mode.height, 4);
    if (!s->buffer.buffer) { s->failed = true; return; }
    uint8_t *jpeg = NULL, dqt[RACER_DQT_SIZE]; size_t length = 0;
    if (encode(s->buffer.buffer, (unsigned)mode.width, (unsigned)mode.height,
               s->quality, &jpeg, &length, dqt)) { s->failed = true; goto done; }
    if (racer_usb_set_quant_table(s->usb, dqt) ||
        racer_usb_set_resolution(s->usb, (uint16_t)mode.width, (uint16_t)mode.height)) {
        fprintf(stderr, "USB configure: %s\n", racer_usb_error(s->usb));
        s->failed = true;
    }
done:
    free(jpeg);
    /* Register even on configuration failure so the cleanup path is symmetric. */
    evdi_register_buffer(s->evdi, s->buffer);
}
static void update_ready(int buffer, void *data)
{
    struct stream *s = data;
    if (s->pending && s->buffer.buffer && buffer == s->buffer.id) s->ready = true;
}
static void dpms_changed(int mode, void *data) { (void)data; fprintf(stderr, "DPMS mode %d\n", mode); }
static void ddcci(struct evdi_ddcci_data request, void *data)
{
    (void)request;
    struct stream *s = data;
    evdi_ddcci_response(s->evdi, NULL, 0, false);
}

static int run_stream(struct racer_usb *usb, const uint8_t *edid, size_t edid_length,
                       unsigned width, unsigned height, int fps, int quality, int seconds,
                       bool stats, bool damage)
{
    struct stream s = {.usb = usb, .native_width = width, .native_height = height, .quality = quality};
    if (!width || !height || width % 16 || height % 8) {
        fprintf(stderr, "Preferred mode must be aligned to 16x8 pixels\n"); return -1;
    }
    s.evdi = evdi_open_attached_to_fixed(NULL, 0);
    if (!s.evdi) {
        fprintf(stderr, "Cannot open EVDI. Load the built module first (see README.ru.md).\n"); return -1;
    }
    struct evdi_event_context events = {
        .dpms_handler = dpms_changed, .mode_changed_handler = mode_changed,
        .update_ready_handler = update_ready, .ddcci_data_handler = ddcci, .user_data = &s
    };
    evdi_enable_cursor_events(s.evdi, false); /* EVDI draws cursor into captured pixels. */
    evdi_connect2(s.evdi, edid, (unsigned)edid_length, width * height, width * height * 60);
    fprintf(stderr, "Virtual monitor connected. Enable it in KDE display settings if needed.\n");
    struct pollfd pfd = {.fd = evdi_get_event_ready(s.evdi), .events = POLLIN};
    const double start = now_seconds();
    double next_frame = start;
    double last_full = start;
    s.stats.start = start;
    fprintf(stderr, "Refresh target: %d fps; partial updates %s.\n", fps,
            damage ? "enabled (experimental)" : "disabled");
    while (!stopping && !s.failed && (!seconds || now_seconds() - start < seconds)) {
        double now = now_seconds();
        if (stats) report_stats(&s, now, fps);
        /* Request damage immediately; throttle its consumption, not its arrival.
         * An idle compositor can leave this request pending without busy polling. */
        if (s.buffer.buffer && !s.pending) {
            s.pending = true;
            s.ready = evdi_request_update(s.evdi, s.buffer.id);
        }
        if (s.ready && s.buffer.buffer && now_seconds() >= next_frame) {
            const double frame_start = now_seconds();
            struct evdi_rect rects[16]; int count = 16;
            evdi_grab_pixels(s.evdi, rects, &count);
            const double grabbed = now_seconds();
            s.pending = s.ready = false;
            if (count > 0) {
                struct bosto_frame_region region = {
                    0, 0, (unsigned)s.buffer.width, (unsigned)s.buffer.height
                };
                if (damage && !s.force_full && frame_start - last_full < 2.0) {
                    struct bosto_damage_rect changed[16];
                    if (count > 16) {
                        fprintf(stderr, "EVDI returned too many dirty rectangles\n");
                        s.failed = true; break;
                    }
                    for (int i = 0; i < count; ++i)
                        changed[i] = (struct bosto_damage_rect){
                            rects[i].x1, rects[i].y1, rects[i].x2, rects[i].y2
                        };
                    int selected = bosto_damage_region(changed, (size_t)count,
                        (unsigned)s.buffer.width, (unsigned)s.buffer.height, &region);
                    if (selected < 0) { s.failed = true; break; }
                    if (!selected) {
                        next_frame = frame_start + 1.0 / fps;
                        continue;
                    }
                }
                uint8_t *jpeg = NULL; size_t length = 0;
                char error[256];
                const uint8_t *pixels = (const uint8_t *)s.buffer.buffer +
                    (size_t)region.y * s.buffer.stride + (size_t)region.x * 4;
                if (bosto_encode_jpeg(pixels, region.width, region.height,
                                      (unsigned)s.buffer.stride, quality,
                                      &jpeg, &length, error, sizeof(error))) {
                    fprintf(stderr, "JPEG: %s\n", error); s.failed = true;
                } else {
                    const double encoded = now_seconds();
                    if (racer_usb_send_jpeg(usb, jpeg, length,
                                           region.width, region.height, region.x, region.y)) {
                        fprintf(stderr, "USB frame: %s\n", racer_usb_error(usb)); s.failed = true;
                    } else {
                        ++s.frames;
                        ++s.stats.frames;
                        s.stats.bytes += length;
                        s.stats.grab += grabbed - frame_start;
                        s.stats.encode += encoded - grabbed;
                        s.stats.usb += now_seconds() - encoded;
                        bool full = region.x == 0 && region.y == 0 &&
                            region.width == (unsigned)s.buffer.width &&
                            region.height == (unsigned)s.buffer.height;
                        if (full) last_full = frame_start;
                        else ++s.stats.partial;
                        s.force_full = false;
                    }
                }
                free(jpeg);
            }
            /* JPEG/USB time is part of the frame period. Do not add a fresh
             * period after it. Slow frames proceed without a catch-up burst. */
            next_frame = frame_start + 1.0 / fps;
        }
        now = now_seconds();
        /* Service mode/DPMS events even when capture is slower than the cap.
         * A completed frame gets a nonblocking event check, then another
         * damage request on the next iteration. */
        double wait = s.buffer.buffer && !s.pending ? 0.0 : 1.0;
        if (s.ready && s.buffer.buffer && next_frame - now < wait)
            wait = next_frame - now;
        if (stats && s.stats.start + 2.0 - now < wait)
            wait = s.stats.start + 2.0 - now;
        if (seconds && start + seconds - now < wait)
            wait = start + seconds - now;
        int timeout_ms = 0;
        if (wait > 0) {
            timeout_ms = (int)(wait * 1000.0);
            if ((double)timeout_ms < wait * 1000.0) ++timeout_ms;
        }
        int result = poll(&pfd, 1, timeout_ms);
        if (result < 0 && errno != EINTR) { perror("poll"); s.failed = true; }
        if (result > 0 && (pfd.revents & (POLLERR | POLLHUP | POLLNVAL))) s.failed = true;
        if (result > 0 && (pfd.revents & POLLIN)) evdi_handle_events(s.evdi, &events);
    }
    evdi_disconnect(s.evdi);
    if (s.buffer.buffer) { evdi_unregister_buffer(s.evdi, s.buffer.id); free(s.buffer.buffer); }
    evdi_close(s.evdi);
    fprintf(stderr, "Stopped; %u frames transferred.\n", s.frames);
    return s.failed ? -1 : 0;
}

static int number(const char *value, int low, int high)
{
    char *end; errno = 0;
    long n = strtol(value, &end, 10);
    return errno || !*value || *end || n < low || n > high ? -1 : (int)n;
}

int main(int argc, char **argv)
{
    enum {NONE, PROBE, PATTERN, RUN, OFFLINE} action = NONE;
    int fps = 60, quality = 90, seconds = 0;
    bool stats = false, damage = false;
    const char *output = NULL;
    for (int i = 1; i < argc; ++i) {
        int selected = NONE;
        if (!strcmp(argv[i], "--help")) { usage(argv[0]); return 0; }
        if (!strcmp(argv[i], "--probe")) selected = PROBE;
        else if (!strcmp(argv[i], "--pattern")) selected = PATTERN;
        else if (!strcmp(argv[i], "--run")) selected = RUN;
        else if (!strcmp(argv[i], "--offline-pattern") && i + 1 < argc) { selected = OFFLINE; output = argv[++i]; }
        else if (!strcmp(argv[i], "--fps") && i + 1 < argc) { fps = number(argv[++i], 1, 60); }
        else if (!strcmp(argv[i], "--quality") && i + 1 < argc) { quality = number(argv[++i], 1, 100); }
        else if (!strcmp(argv[i], "--seconds") && i + 1 < argc) { seconds = number(argv[++i], 1, 86400); }
        else if (!strcmp(argv[i], "--stats")) stats = true;
        else if (!strcmp(argv[i], "--damage")) damage = true;
        else { fprintf(stderr, "Unknown or incomplete option: %s\n", argv[i]); return 2; }
        if (selected) { if (action) { fprintf(stderr, "Choose one action\n"); return 2; } action = selected; }
    }
    if (fps < 0 || quality < 0 || seconds < 0) { fprintf(stderr, "Option value out of range\n"); return 2; }
    if (!action) { usage(argv[0]); return 0; }
    struct sigaction sa = {.sa_handler = stop_handler};
    sigemptyset(&sa.sa_mask); sigaction(SIGINT, &sa, NULL); sigaction(SIGTERM, &sa, NULL);
    setvbuf(stdout, NULL, _IOLBF, 0);
    uint8_t *pixels = NULL, *jpeg = NULL, dqt[RACER_DQT_SIZE]; size_t length = 0;
    if (action == PATTERN || action == OFFLINE) {
        pixels = bosto_test_pattern(1280, 800);
        if (!pixels || encode(pixels, 1280, 800, quality, &jpeg, &length, dqt)) { free(pixels); return 1; }
        free(pixels);
        if (action == OFFLINE) {
            FILE *f = fopen(output, "wb");
            if (!f) { perror(output); free(jpeg); return 1; }
            int failed = fwrite(jpeg, 1, length, f) != length;
            if (fclose(f)) failed = 1;
            printf("Offline JPEG: %zu bytes, 1280x800, YUV 4:2:2, quality %d\n", length, quality);
            free(jpeg); return failed;
        }
    }
    struct racer_usb usb;
    if (racer_usb_open(&usb)) { fprintf(stderr, "%s\n", racer_usb_error(&usb)); free(jpeg); return 1; }
    int result = 0;
    if (action == PATTERN) {
        uint8_t edid[512]; size_t edid_length; unsigned width, height;
        result = read_edid(&usb, edid, &edid_length, &width, &height);
        if (!result && (width != 1280 || height != 800)) {
            fprintf(stderr, "Test pattern expects the BT-101A native mode 1280x800; refusing %ux%u\n", width, height);
            result = -1;
        }
        if (!result) result = racer_usb_set_quant_table(&usb, dqt) || racer_usb_set_resolution(&usb, 1280, 800) ||
                              racer_usb_send_jpeg(&usb, jpeg, length, 1280, 800, 0, 0);
        if (!result) printf("1280x800 test pattern transferred (%zu JPEG bytes). Check the tablet screen.\n", length);
    } else {
        uint16_t status;
        if (!racer_usb_get_status(&usb, &status)) printf("USB display status: 0x%04x\n", status);
        uint8_t edid[512]; size_t edid_length; unsigned width, height;
        result = read_edid(&usb, edid, &edid_length, &width, &height);
        if (!result && action == RUN) result = run_stream(&usb, edid, edid_length, width, height,
                                                        fps, quality, seconds, stats, damage);
    }
    if (result) fprintf(stderr, "Display operation failed: %s\n", racer_usb_error(&usb));
    racer_usb_close(&usb); free(jpeg);
    return result ? 1 : 0;
}
