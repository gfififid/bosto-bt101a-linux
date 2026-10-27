/* SPDX-License-Identifier: MIT */
#include "frame_region.h"
#include <limits.h>
#include <stdint.h>

int bosto_damage_region(const struct bosto_damage_rect *damage, size_t count,
                        unsigned width, unsigned height,
                        struct bosto_frame_region *region)
{
    if (!region || (!damage && count) || !width || !height ||
        width > INT_MAX - 15u || height > INT_MAX - 7u ||
        (width & 15u) || (height & 7u))
        return -1;
    int left = (int)width, top = (int)height, right = 0, bottom = 0;
    for (size_t i = 0; i < count; ++i) {
        int x1 = damage[i].x1 < 0 ? 0 : damage[i].x1;
        int y1 = damage[i].y1 < 0 ? 0 : damage[i].y1;
        int x2 = damage[i].x2 > (int)width ? (int)width : damage[i].x2;
        int y2 = damage[i].y2 > (int)height ? (int)height : damage[i].y2;
        if (x2 <= x1 || y2 <= y1) continue;
        if (x1 < left) left = x1;
        if (y1 < top) top = y1;
        if (x2 > right) right = x2;
        if (y2 > bottom) bottom = y2;
    }
    if (right <= left || bottom <= top) return 0;
    region->x = (unsigned)left & ~15u;
    region->y = (unsigned)top & ~7u;
    region->width = (((unsigned)right + 15u) & ~15u) - region->x;
    region->height = (((unsigned)bottom + 7u) & ~7u) - region->y;
    /* Above 75%, a full frame simplifies transport with little extra work. */
    if ((uint64_t)region->width * region->height * 4 >=
        (uint64_t)width * height * 3) {
        region->x = region->y = 0;
        region->width = width;
        region->height = height;
    }
    return 1;
}
