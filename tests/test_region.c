/* SPDX-License-Identifier: MIT */
#include "frame_region.h"
#include <limits.h>
#include <stdio.h>

#define CHECK(condition) do { if (!(condition)) { \
    fprintf(stderr, "Damage region failed at line %d\n", __LINE__); return 1; \
} } while (0)

int main(void)
{
    struct bosto_frame_region r;
    struct bosto_damage_rect small[] = {{17,9,18,10}};
    CHECK(bosto_damage_region(small, 1, 1280, 800, &r) == 1);
    CHECK(r.x == 16 && r.y == 8 && r.width == 16 && r.height == 8);
    struct bosto_damage_rect edge[] = {{1279,799,INT_MAX,INT_MAX}};
    CHECK(bosto_damage_region(edge, 1, 1280, 800, &r) == 1);
    CHECK(r.x == 1264 && r.y == 792 && r.width == 16 && r.height == 8);
    struct bosto_damage_rect unioned[] = {{-5,-8,10,10}, {17,9,100,30}, {99,10,20,19}};
    CHECK(bosto_damage_region(unioned, 3, 1280, 800, &r) == 1);
    CHECK(r.x == 0 && r.y == 0 && r.width == 112 && r.height == 32);
    struct bosto_damage_rect invalid[] = {{INT_MIN,0,-1,20}, {1400,0,1500,20}, {20,30,19,10}};
    CHECK(bosto_damage_region(invalid, 3, 1280, 800, &r) == 0);
    struct bosto_damage_rect full[] = {{0,0,1280,799}};
    CHECK(bosto_damage_region(full, 1, 1280, 800, &r) == 1);
    CHECK(r.x == 0 && r.y == 0 && r.width == 1280 && r.height == 800);
    CHECK(bosto_damage_region(NULL, 0, 1280, 800, &r) == 0);
    CHECK(bosto_damage_region(NULL, 1, 1280, 800, &r) == -1);
    CHECK(bosto_damage_region(full, 1, 1279, 800, &r) == -1);
    CHECK(bosto_damage_region(full, 1, 1280, 800, NULL) == -1);
    puts("Damage union: alignment, clipping, edge overflow and fallback checks passed");
    return 0;
}
