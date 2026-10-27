/* SPDX-License-Identifier: MIT */
#include "jpeg_frame.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <setjmp.h>
#include <jpeglib.h>
#include <jerror.h>

struct bosto_jpeg_error { struct jpeg_error_mgr base; jmp_buf jump; char message[JMSG_LENGTH_MAX]; };
/* All state that libjpeg changes after setjmp lives on the heap. C makes
 * changed, non-volatile automatic variables indeterminate after longjmp. */
struct bosto_jpeg_context {
    struct jpeg_compress_struct info;
    struct bosto_jpeg_error error;
    struct jpeg_destination_mgr destination;
    unsigned char *output;
    size_t output_length, capacity;
};
static void set_error(char *error, size_t error_size, const char *message)
{
    if (error && error_size) snprintf(error, error_size, "%s", message);
}
static void jpeg_failure(j_common_ptr info)
{
    struct bosto_jpeg_error *e = (struct bosto_jpeg_error *)info->err;
    e->base.format_message(info, e->message);
    longjmp(e->jump, 1);
}
static void destination_init(j_compress_ptr info)
{
    struct bosto_jpeg_context *context = info->client_data;
    context->destination.next_output_byte = context->output;
    context->destination.free_in_buffer = context->capacity;
}
static boolean destination_grow(j_compress_ptr info)
{
    struct bosto_jpeg_context *context = info->client_data;
    if (context->capacity > SIZE_MAX / 2) ERREXIT(info, JERR_OUT_OF_MEMORY);
    size_t next_capacity = context->capacity * 2;
    uint8_t *next = realloc(context->output, next_capacity);
    if (!next) ERREXIT(info, JERR_OUT_OF_MEMORY);
    context->output = next;
    context->destination.next_output_byte = next + context->capacity;
    context->destination.free_in_buffer = next_capacity - context->capacity;
    context->capacity = next_capacity;
    return TRUE;
}
static void destination_finish(j_compress_ptr info)
{
    struct bosto_jpeg_context *context = info->client_data;
    context->output_length = context->capacity - context->destination.free_in_buffer;
}

int bosto_encode_jpeg(const uint8_t *bgrx, unsigned width, unsigned height,
                      unsigned stride, int quality, uint8_t **jpeg,
                      size_t *length, char *error, size_t error_size)
{
    if (jpeg) *jpeg = NULL;
    if (length) *length = 0;
    if (error && error_size) error[0] = '\0';
    if (!bgrx || !jpeg || !length || !width || !height || width > 4096 ||
        height > 4096 || stride < width * 4 ||
        (size_t)stride > SIZE_MAX / height || quality < 1 || quality > 100) {
        set_error(error, error_size, "Invalid JPEG dimensions/stride/quality");
        return -1;
    }
    struct bosto_jpeg_context *context = calloc(1, sizeof(*context));
    if (!context) {
        set_error(error, error_size, "Cannot allocate JPEG encoder state");
        return -1;
    }
    context->capacity = 4096;
    context->output = malloc(context->capacity);
    if (!context->output) {
        set_error(error, error_size, "Cannot allocate JPEG output buffer");
        free(context);
        return -1;
    }
    context->info.err = jpeg_std_error(&context->error.base);
    context->error.base.error_exit = jpeg_failure;
    if (setjmp(context->error.jump)) {
        jpeg_destroy_compress(&context->info);
        free(context->output);
        set_error(error, error_size, context->error.message);
        free(context);
        return -1;
    }
    jpeg_create_compress(&context->info);
    /* jpeg_mem_dest retains its current reallocations privately until finish,
     * making an error after growth difficult to clean up. Keep ownership here. */
    context->info.client_data = context;
    context->destination.init_destination = destination_init;
    context->destination.empty_output_buffer = destination_grow;
    context->destination.term_destination = destination_finish;
    context->info.dest = &context->destination;
    context->info.image_width = width;
    context->info.image_height = height;
    context->info.input_components = 4;
    context->info.in_color_space = JCS_EXT_BGRX;
    jpeg_set_defaults(&context->info);
    jpeg_set_quality(&context->info, quality, TRUE);
    /* Match the Mac driver's baseline JPEG YUV 4:2:2 mode. */
    context->info.comp_info[0].h_samp_factor = 2;
    context->info.comp_info[0].v_samp_factor = 1;
    context->info.comp_info[1].h_samp_factor = context->info.comp_info[1].v_samp_factor = 1;
    context->info.comp_info[2].h_samp_factor = context->info.comp_info[2].v_samp_factor = 1;
    context->info.optimize_coding = FALSE;
    jpeg_start_compress(&context->info, TRUE);
    while (context->info.next_scanline < context->info.image_height) {
        JSAMPROW row = (JSAMPROW)(bgrx + (size_t)context->info.next_scanline * stride);
        jpeg_write_scanlines(&context->info, &row, 1);
    }
    jpeg_finish_compress(&context->info);
    jpeg_destroy_compress(&context->info);
    *jpeg = context->output;
    *length = context->output_length;
    free(context);
    return 0;
}

int bosto_extract_dqt(const uint8_t *jpeg, size_t length, uint8_t dqt[138])
{
    unsigned seen = 0;
    size_t offset = 2;
    uint8_t extracted[138];
    if (!jpeg || !dqt || length < 4 || jpeg[0] != 0xff || jpeg[1] != 0xd8) return -1;
    while (offset + 4 <= length) {
        if (jpeg[offset++] != 0xff) return -1;
        while (offset < length && jpeg[offset] == 0xff) ++offset;
        if (offset >= length) return -1;
        unsigned marker = jpeg[offset++];
        if (marker == 0xd9 || marker == 0xd8 || marker == 0x00) return -1;
        if (marker == 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue;
        if (offset + 2 > length) return -1;
        size_t segment = ((size_t)jpeg[offset] << 8) | jpeg[offset + 1];
        if (segment < 2 || segment > length - offset) return -1;
        if (marker == 0xda) {
            /* SOS is the last header segment; its complete header must fit.
             * Entropy data/EOI validation is the decoder's responsibility. */
            if (segment < 6 || seen != 3) return -1;
            memcpy(dqt, extracted, sizeof(extracted));
            return 0;
        }
        if (marker == 0xdb) {
            size_t p = offset + 2;
            while (p < offset + segment) {
                unsigned spec = jpeg[p++];
                if (spec > 1 || offset + segment - p < 64) return -1;
                unsigned bit = 1u << spec;
                if (seen & bit) return -1;
                uint8_t *out = extracted + 69 * spec;
                const uint8_t header[] = {0xff, 0xdb, 0x00, 0x43, (uint8_t)spec};
                memcpy(out, header, 5);
                memcpy(out + 5, jpeg + p, 64);
                p += 64;
                seen |= bit;
            }
        }
        offset += segment;
    }
    return -1;
}

uint8_t *bosto_test_pattern(unsigned width, unsigned height)
{
    static const uint8_t colors[8][3] = {
        {255,255,255}, {255,255,0}, {0,255,255}, {0,255,0},
        {255,0,255}, {255,0,0}, {0,0,255}, {0,0,0}
    };
    if (!width || !height || width > 4096 || height > 4096) return NULL;
    uint8_t *pixels = malloc((size_t)width * height * 4);
    if (!pixels) return NULL;
    for (unsigned y = 0; y < height; ++y) for (unsigned x = 0; x < width; ++x) {
        uint8_t *p = pixels + ((size_t)y * width + x) * 4;
        unsigned c = x * 8 / width;
        p[0] = colors[c][2]; p[1] = colors[c][1]; p[2] = colors[c][0]; p[3] = 255;
        if (y > height * 3 / 4) p[0] = p[1] = p[2] = width > 1 ? (uint8_t)(x * 255 / (width - 1)) : 0;
        if (x < 4 || y < 4 || x + 4 >= width || y + 4 >= height) p[0] = p[1] = p[2] = 255;
    }
    return pixels;
}
