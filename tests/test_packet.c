/* SPDX-License-Identifier: MIT */
/* Golden wire-format test. No USB device is opened. */
#include "racer_usb.h"
#include <stdint.h>
#include <stdio.h>
#include <string.h>

int main(void)
{
    uint8_t packet[RACER_HEADER_SIZE];
    /* payload 0x12345 -> total 0x12365, rectangle 1280x800 at (16,8). */
    static const uint8_t expected[RACER_HEADER_SIZE] = {
        0x58,0x58,0x65,0x23,0x01,0x00,0x85,0x85,
        0x45,0x23,0x01,0x00,0xc0,0xde,0x00,0x05,
        0x00,0x00,0x20,0x03,0x00,0x00,0x10,0x00,
        0x00,0x00,0x08,0x00,0x00,0x00,0xfe,0xed
    };
    if (racer_usb_pack_header(packet, 0x12345, 1280, 800, 16, 8) ||
        memcmp(packet, expected, sizeof(expected))) {
        fputs("MI8808 packet differs from the recovered wire format\n", stderr);
        return 1;
    }
    if (racer_usb_pack_header(packet, UINT32_MAX - RACER_HEADER_SIZE,
                              1280, 800, 0, 0) ||
        packet[2] != 0xff || packet[3] != 0xff ||
        packet[4] != 0xff || packet[5] != 0xff) {
        fputs("MI8808 maximum packet length was not encoded correctly\n", stderr);
        return 1;
    }
    if (racer_usb_pack_header(packet, UINT32_MAX - RACER_HEADER_SIZE + (size_t)1,
                              1280, 800, 0, 0) != -1 ||
        racer_usb_pack_header(NULL, 100, 1280, 800, 0, 0) != -1) {
        fputs("MI8808 invalid packet length or buffer was accepted\n", stderr);
        return 1;
    }
    puts("MI8808 header: golden bytes and overflow checks passed");
    return 0;
}
