/* SPDX-License-Identifier: MIT */
#ifndef BOSTO_FRAME_REGION_H
#define BOSTO_FRAME_REGION_H
#include <stddef.h>
struct bosto_damage_rect { int x1, y1, x2, y2; };
struct bosto_frame_region { unsigned x, y, width, height; };
/* Build one clipped JPEG-MCU-aligned union. 1=region, 0=no valid damage,
 * -1=invalid dimensions/arguments. Large unions become a full-frame update. */
int bosto_damage_region(const struct bosto_damage_rect *damage, size_t count,
                        unsigned width, unsigned height,
                        struct bosto_frame_region *region);
#endif
