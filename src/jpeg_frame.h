#ifndef BOSTO_JPEG_FRAME_H
#define BOSTO_JPEG_FRAME_H
#include <stddef.h>
#include <stdint.h>
/* Encode B,G,R,unused pixels as baseline YCbCr JPEG 4:2:2. On success the
 * caller owns *jpeg (free()); on error valid output pointers are reset. */
int bosto_encode_jpeg(const uint8_t *bgrx, unsigned width, unsigned height,
                      unsigned stride, int quality, uint8_t **jpeg,
                      size_t *length, char *error, size_t error_size);
/* Extract exactly two 8-bit DQT tables (IDs 0/1) into separate 69-byte
 * marker segments. Reject truncated headers; leave dqt unchanged on failure. */
int bosto_extract_dqt(const uint8_t *jpeg, size_t length, uint8_t dqt[138]);
uint8_t *bosto_test_pattern(unsigned width, unsigned height);
#endif
