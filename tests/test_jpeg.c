/* SPDX-License-Identifier: MIT */
#include "jpeg_frame.h"
#include <assert.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <jpeglib.h>
#include <jerror.h>

#ifdef BOSTO_TEST_JPEG_FAILURE
/* Link with --wrap=jpeg_write_scanlines to exercise libjpeg's fatal-error
 * recovery after the destination has grown, without exhausting system memory. */
static int fail_after_scanline = -1;
JDIMENSION __real_jpeg_write_scanlines(j_compress_ptr, JSAMPARRAY, JDIMENSION);
JDIMENSION __wrap_jpeg_write_scanlines(j_compress_ptr info, JSAMPARRAY rows,
                                      JDIMENSION count)
{
    if (fail_after_scanline >= 0 && info->next_scanline >= (unsigned)fail_after_scanline)
        ERREXIT(info, JERR_FILE_WRITE);
    return __real_jpeg_write_scanlines(info, rows, count);
}
#endif

static void check_channel(unsigned actual, unsigned expected)
{
    assert(actual + 8 >= expected && expected + 8 >= actual);
}

static void test_encode_and_decode(void)
{
    enum { W = 32, H = 16, STRIDE = W * 4 + 12 };
    uint8_t pixels[H * STRIDE];
    memset(pixels, 0xa5, sizeof(pixels));
    for (unsigned y = 0; y < H; ++y) for (unsigned x = 0; x < W; ++x) {
        uint8_t *p = pixels + y * STRIDE + x * 4;
        p[0] = x < W / 2 ? 0 : 255; /* Left red, right blue, BGRX input. */
        p[1] = 0;
        p[2] = x < W / 2 ? 255 : 0;
        p[3] = (uint8_t)(x + y);
    }
    uint8_t *jpeg = NULL;
    size_t length = 0;
    char error[128];
    assert(bosto_encode_jpeg(pixels, W, H, STRIDE, 100, &jpeg, &length,
                             error, sizeof(error)) == 0);
    assert(jpeg && length > 100 && error[0] == '\0');
    uint8_t tables[138];
    assert(bosto_extract_dqt(jpeg, length, tables) == 0);
    assert(!memcmp(tables, "\xff\xdb\x00\x43\x00", 5));
    assert(!memcmp(tables + 69, "\xff\xdb\x00\x43\x01", 5));

    struct jpeg_decompress_struct info = {0};
    struct jpeg_error_mgr e;
    info.err = jpeg_std_error(&e);
    jpeg_create_decompress(&info);
    jpeg_mem_src(&info, jpeg, (unsigned long)length);
    assert(jpeg_read_header(&info, TRUE) == JPEG_HEADER_OK);
    assert(info.image_width == W && info.image_height == H);
    assert(info.num_components == 3 && info.data_precision == 8);
    assert(!info.progressive_mode && !info.arith_code);
    assert(info.comp_info[0].h_samp_factor == 2);
    assert(info.comp_info[0].v_samp_factor == 1);
    for (int c = 1; c < 3; ++c) {
        assert(info.comp_info[c].h_samp_factor == 1);
        assert(info.comp_info[c].v_samp_factor == 1);
    }
    info.out_color_space = JCS_RGB;
    assert(jpeg_start_decompress(&info));
    assert(info.output_components == 3);
    uint8_t row[W * 3];
    while (info.output_scanline < info.output_height) {
        JSAMPROW p = row;
        assert(jpeg_read_scanlines(&info, &p, 1) == 1);
        check_channel(row[4 * 3], 255);
        check_channel(row[4 * 3 + 1], 0);
        check_channel(row[4 * 3 + 2], 0);
        check_channel(row[24 * 3], 0);
        check_channel(row[24 * 3 + 1], 0);
        check_channel(row[24 * 3 + 2], 255);
    }
    assert(jpeg_finish_decompress(&info));
    jpeg_destroy_decompress(&info);

    /* The unused X byte and padding must not change encoded colors. */
    for (unsigned y = 0; y < H; ++y) for (unsigned x = 0; x < W; ++x)
        pixels[y * STRIDE + x * 4 + 3] = 255;
    uint8_t *same = NULL; size_t same_length = 0;
    assert(bosto_encode_jpeg(pixels, W, H, STRIDE, 100, &same, &same_length,
                             NULL, 0) == 0);
    assert(same_length == length && !memcmp(jpeg, same, length));
    free(same);

    /* Every truncation before the complete SOS header must fail, including
     * truncations after both DQT tables. The output stays unchanged on error. */
    size_t offset = 2, header_end = 0;
    while (offset + 4 <= length) {
        assert(jpeg[offset] == 0xff);
        unsigned marker = jpeg[offset + 1];
        size_t segment = ((size_t)jpeg[offset + 2] << 8) | jpeg[offset + 3];
        if (marker == 0xda) { header_end = offset + 2 + segment; break; }
        offset += 2 + segment;
    }
    assert(header_end && header_end < length);
    for (size_t n = 0; n < header_end; ++n) {
        memset(tables, 0x5a, sizeof(tables));
        assert(bosto_extract_dqt(jpeg, n, tables) == -1);
        for (size_t i = 0; i < sizeof(tables); ++i) assert(tables[i] == 0x5a);
    }
    assert(bosto_extract_dqt(jpeg, header_end, tables) == 0);
    free(jpeg);
}

static void test_dqt_segments(void)
{
    /* Both tables in one legal DQT segment, plus a complete one-component
     * scan header. This test concerns header extraction, not image decoding. */
    uint8_t combined[146] = {0xff,0xd8,0xff,0xdb,0x00,0x84};
    combined[6] = 0; memset(combined + 7, 11, 64);
    combined[71] = 1; memset(combined + 72, 17, 64);
    const uint8_t sos[] = {0xff,0xda,0x00,0x08,0x01,0x01,0x00,0x00,0x3f,0x00};
    memcpy(combined + 136, sos, sizeof(sos));
    uint8_t tables[138];
    assert(bosto_extract_dqt(combined, sizeof(combined), tables) == 0);
    for (size_t i = 5; i < 69; ++i) assert(tables[i] == 11);
    for (size_t i = 74; i < 138; ++i) assert(tables[i] == 17);

    combined[6] = 0x10; /* 16-bit quantization is outside the transport format. */
    assert(bosto_extract_dqt(combined, sizeof(combined), tables) == -1);
    combined[6] = 2; /* Unsupported table ID. */
    assert(bosto_extract_dqt(combined, sizeof(combined), tables) == -1);
    combined[6] = 0;
    combined[71] = 0; /* Duplicate table, missing chroma table. */
    assert(bosto_extract_dqt(combined, sizeof(combined), tables) == -1);
    combined[71] = 1;
    combined[4] = 0; combined[5] = 1; /* Invalid segment length. */
    assert(bosto_extract_dqt(combined, sizeof(combined), tables) == -1);
    assert(bosto_extract_dqt(NULL, 0, tables) == -1);
    assert(bosto_extract_dqt(combined, sizeof(combined), NULL) == -1);
}

static void test_guards(void)
{
    uint8_t pixel[4] = {0};
    const unsigned cases[][4] = {
        {0,1,4,90}, {1,0,4,90}, {4097,1,16388,90}, {1,4097,4,90},
        {2,1,4,90}, {1,1,4,0}, {1,1,4,101}, {UINT_MAX,1,4,90}
    };
    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); ++i) {
        uint8_t *jpeg = pixel; size_t length = 123;
        char error[8];
        assert(bosto_encode_jpeg(pixel, cases[i][0], cases[i][1], cases[i][2],
                                 (int)cases[i][3], &jpeg, &length, error, sizeof(error)) == -1);
        assert(!jpeg && !length && error[0] && error[sizeof(error) - 1] == '\0');
    }
    uint8_t *jpeg = pixel; size_t length = 123;
    assert(bosto_encode_jpeg(NULL, 1, 1, 4, 90, &jpeg, &length, NULL, 0) == -1);
    assert(!jpeg && !length);
    assert(bosto_encode_jpeg(pixel, 1, 1, 4, 90, NULL, &length, NULL, 3) == -1);
    assert(bosto_encode_jpeg(pixel, 1, 1, 4, 90, &jpeg, NULL, NULL, 0) == -1);

    assert(!bosto_test_pattern(0,1));
    assert(!bosto_test_pattern(1,0));
    assert(!bosto_test_pattern(4097,1));
    /* The one-pixel gradient used to divide by width-1. */
    uint8_t *pattern = bosto_test_pattern(1, 16);
    assert(pattern);
    free(pattern);
}

static void test_output_growth_and_recovery(void)
{
    enum { W = 256, H = 256 };
    uint8_t *pixels = malloc(W * H * 4);
    assert(pixels);
    uint32_t random = 1;
    for (size_t i = 0; i < W * H * 4; ++i) {
        random ^= random << 13; random ^= random >> 17; random ^= random << 5;
        pixels[i] = (uint8_t)random;
    }
    uint8_t *jpeg = NULL; size_t length = 0;
    char error[128];
#ifdef BOSTO_TEST_JPEG_FAILURE
    fail_after_scanline = H / 2;
    assert(bosto_encode_jpeg(pixels, W, H, W * 4, 100, &jpeg, &length,
                             error, sizeof(error)) == -1);
    assert(!jpeg && !length && error[0]);
    fail_after_scanline = -1;
#endif
    assert(bosto_encode_jpeg(pixels, W, H, W * 4, 100, &jpeg, &length,
                             error, sizeof(error)) == 0);
    assert(jpeg && length > 4096); /* Exercise repeated destination reallocations. */
    uint8_t tables[138];
    assert(bosto_extract_dqt(jpeg, length, tables) == 0);
    free(jpeg);
    free(pixels);
}

int main(void)
{
    test_encode_and_decode();
    test_dqt_segments();
    test_guards();
    test_output_growth_and_recovery();
    puts("JPEG tests passed");
    return 0;
}
