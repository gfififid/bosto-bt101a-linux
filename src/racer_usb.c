/* SPDX-License-Identifier: MIT */
#define _POSIX_C_SOURCE 200809L
#include "racer_usb.h"

#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define CONTROL_TIMEOUT_MS 1000u
#define BULK_TIMEOUT_MS 2000u

static int fail(struct racer_usb *usb, const char *format, ...)
{
    va_list args;
    va_start(args, format);
    vsnprintf(usb->error, sizeof(usb->error), format, args);
    va_end(args);
    return -1;
}

const char *racer_usb_error(const struct racer_usb *usb)
{
    return usb->error[0] ? usb->error : "No USB error";
}

void racer_usb_close(struct racer_usb *usb)
{
    if (usb->handle) {
        if (usb->claimed)
            libusb_release_interface(usb->handle, RACER_INTERFACE);
        libusb_close(usb->handle);
    }
    if (usb->context)
        libusb_exit(usb->context);
    usb->handle = NULL;
    usb->context = NULL;
    usb->claimed = 0;
}

static int validate_interface(struct racer_usb *usb)
{
    struct libusb_config_descriptor *config = NULL;
    int result = libusb_get_active_config_descriptor(
        libusb_get_device(usb->handle), &config);
    if (result < 0)
        return fail(usb, "Read active USB configuration: %s",
                    libusb_error_name(result));
    int found = 0;
    for (uint8_t i = 0; i < config->bNumInterfaces; ++i) {
        const struct libusb_interface *interface = &config->interface[i];
        for (int a = 0; a < interface->num_altsetting; ++a) {
            const struct libusb_interface_descriptor *setting =
                &interface->altsetting[a];
            if (setting->bInterfaceNumber != RACER_INTERFACE ||
                setting->bAlternateSetting != 0 ||
                setting->bInterfaceClass != LIBUSB_CLASS_VENDOR_SPEC)
                continue;
            for (uint8_t e = 0; e < setting->bNumEndpoints; ++e) {
                const struct libusb_endpoint_descriptor *endpoint =
                    &setting->endpoint[e];
                if (endpoint->bEndpointAddress == RACER_ENDPOINT_OUT &&
                    (endpoint->bmAttributes & LIBUSB_TRANSFER_TYPE_MASK) ==
                        LIBUSB_TRANSFER_TYPE_BULK)
                    found = 1;
            }
        }
    }
    libusb_free_config_descriptor(config);
    return found ? 0 : fail(usb,
        "USB 34c7:2700 has no expected vendor interface 0 / bulk OUT 0x01; refusing output");
}

int racer_usb_open(struct racer_usb *usb)
{
    memset(usb, 0, sizeof(*usb));
    int result = libusb_init(&usb->context);
    if (result < 0) {
        usb->context = NULL;
        return fail(usb, "Initialize libusb: %s", libusb_error_name(result));
    }
    libusb_device **devices = NULL;
    ssize_t count = libusb_get_device_list(usb->context, &devices);
    if (count < 0) {
        fail(usb, "Enumerate USB devices: %s", libusb_error_name((int)count));
        racer_usb_close(usb);
        return -1;
    }
    libusb_device *selected = NULL;
    unsigned matches = 0;
    for (ssize_t i = 0; i < count; ++i) {
        struct libusb_device_descriptor descriptor;
        if (libusb_get_device_descriptor(devices[i], &descriptor) == 0 &&
            descriptor.idVendor == RACER_VID &&
            descriptor.idProduct == RACER_PID) {
            selected = devices[i];
            ++matches;
        }
    }
    if (matches != 1) {
        fail(usb, matches ? "Multiple USB 34c7:2700 displays found; connect one for this prototype"
                          : "USB display 34c7:2700 was not found");
        libusb_free_device_list(devices, 1);
        racer_usb_close(usb);
        return -1;
    }
    result = libusb_open(selected, &usb->handle);
    libusb_free_device_list(devices, 1);
    if (result < 0) {
        fail(usb, "Open USB 34c7:2700: %s%s", libusb_error_name(result),
             result == LIBUSB_ERROR_ACCESS ? " (run this USB action with sudo)" : "");
        racer_usb_close(usb);
        return -1;
    }
    if (validate_interface(usb) < 0) {
        racer_usb_close(usb);
        return -1;
    }
    return 0;
}

static int claim_output(struct racer_usb *usb)
{
    if (!usb->handle)
        return fail(usb, "USB display is not open");
    if (usb->claimed)
        return 0;
    int result = libusb_kernel_driver_active(usb->handle, RACER_INTERFACE);
    if (result == 1)
        return fail(usb, "Interface 0 already has a kernel driver; refusing to detach it");
    if (result < 0 && result != LIBUSB_ERROR_NOT_SUPPORTED)
        return fail(usb, "Check interface 0: %s", libusb_error_name(result));
    result = libusb_claim_interface(usb->handle, RACER_INTERFACE);
    if (result < 0)
        return fail(usb, "Claim display interface 0: %s", libusb_error_name(result));
    usb->claimed = 1;
    return 0;
}

static int control(struct racer_usb *usb, uint8_t direction,
                   uint8_t request, uint16_t value, uint8_t *data, uint16_t length)
{
    if (!usb->handle)
        return fail(usb, "USB display is not open");
    int result = libusb_control_transfer(usb->handle,
        direction | LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_RECIPIENT_INTERFACE,
        request, value, RACER_INTERFACE, data, length, CONTROL_TIMEOUT_MS);
    if (result < 0)
        return fail(usb, "Control %02x/%02x value %u: %s",
                    direction | LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_RECIPIENT_INTERFACE,
                    request, value, libusb_error_name(result));
    if (result != length)
        return fail(usb, "Control %02x returned %d of %u bytes",
                    request, result, length);
    return 0;
}

static int query_u16(struct racer_usb *usb, uint8_t request, uint16_t *value)
{
    uint8_t bytes[2] = {0};
    if (!value)
        return fail(usb, "Missing query result buffer");
    if (control(usb, LIBUSB_ENDPOINT_IN, request, 0, bytes, sizeof(bytes)) < 0)
        return -1;
    *value = (uint16_t)(bytes[0] | ((uint16_t)bytes[1] << 8));
    return 0;
}

int racer_usb_get_status(struct racer_usb *usb, uint16_t *status)
{
    /* BT-101A firmware returns one status byte to the Mac driver's two-byte
     * request. A short IN transfer is valid USB behavior; retain a zero high
     * byte. Keep EDID and all output lengths strict. */
    uint8_t bytes[2] = {0};
    if (!usb->handle || !status)
        return fail(usb, "USB display is not open or status buffer is missing");
    int result = libusb_control_transfer(usb->handle, 0xc1, 0x40, 0,
        RACER_INTERFACE, bytes, sizeof(bytes), CONTROL_TIMEOUT_MS);
    if (result < 0)
        return fail(usb, "Read display status: %s", libusb_error_name(result));
    if (result != 1 && result != 2)
        return fail(usb, "Status returned %d bytes; expected 1 or 2", result);
    *status = (uint16_t)(bytes[0] | ((uint16_t)bytes[1] << 8));
    return 0;
}

int racer_usb_get_product_type(struct racer_usb *usb, uint16_t *type)
{
    return query_u16(usb, 0x51, type);
}

int racer_usb_get_features(struct racer_usb *usb, uint16_t *features)
{
    return query_u16(usb, 0x52, features);
}

int racer_usb_read_edid(struct racer_usb *usb, uint8_t *edid,
                        size_t capacity, size_t *length)
{
    uint8_t size_bytes[2] = {0};
    static const uint8_t magic[8] = {0,255,255,255,255,255,255,0};
    if (!edid || !length)
        return fail(usb, "Missing EDID output buffer");
    *length = 0;
    if (control(usb, LIBUSB_ENDPOINT_IN, 0x41, 0,
                size_bytes, sizeof(size_bytes)) < 0)
        return -1;
    size_t size = size_bytes[0] | ((size_t)size_bytes[1] << 8);
    if (size < 128 || size % 128 || size > 4096 || size > capacity)
        return fail(usb, "Invalid or oversized EDID length: %zu (capacity %zu)", size, capacity);
    for (size_t offset = 0; offset < size; offset += 128) {
        if (control(usb, LIBUSB_ENDPOINT_IN, 0x41,
                    (uint16_t)(offset / 128 + 1), edid + offset, 128) < 0)
            return -1;
        unsigned checksum = 0;
        for (size_t i = 0; i < 128; ++i)
            checksum += edid[offset + i];
        if (checksum & 255u)
            return fail(usb, "EDID block %zu checksum is invalid", offset / 128);
    }
    if (memcmp(edid, magic, sizeof(magic)))
        return fail(usb, "EDID header is invalid");
    if (((size_t)edid[126] + 1) * 128 > size)
        return fail(usb, "EDID advertises missing extension blocks");
    *length = size;
    return 0;
}

static int wait_ready(struct racer_usb *usb)
{
    struct timespec deadline;
    if (clock_gettime(CLOCK_MONOTONIC, &deadline) != 0)
        return fail(usb, "Read monotonic clock");
    deadline.tv_sec += 2;
    uint16_t status = UINT16_MAX;
    for (;;) {
        if (racer_usb_get_status(usb, &status) < 0)
            return -1;
        if (status == 0)
            return 0;
        struct timespec now;
        if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
            return fail(usb, "Read monotonic clock");
        if (now.tv_sec > deadline.tv_sec ||
            (now.tv_sec == deadline.tv_sec && now.tv_nsec >= deadline.tv_nsec))
            return fail(usb, "Display stayed busy (status 0x%04x) for two seconds", status);
        struct timespec pause = {.tv_sec = 0, .tv_nsec = 10000000};
        nanosleep(&pause, NULL);
    }
}

int racer_usb_set_quant_table(struct racer_usb *usb, const uint8_t dqt[RACER_DQT_SIZE])
{
    static const uint8_t prefix0[5] = {0xff,0xdb,0,0x43,0};
    static const uint8_t prefix1[5] = {0xff,0xdb,0,0x43,1};
    if (!dqt || memcmp(dqt, prefix0, 5) || memcmp(dqt + 69, prefix1, 5))
        return fail(usb, "Expected two 8-bit JPEG DQT segments for tables 0 and 1 (138 bytes)");
    if (claim_output(usb) < 0 || wait_ready(usb) < 0)
        return -1;
    return control(usb, LIBUSB_ENDPOINT_OUT, 0x83, 1,
                    (uint8_t *)dqt, RACER_DQT_SIZE);
}

int racer_usb_set_resolution(struct racer_usb *usb, uint16_t width, uint16_t height)
{
    if (!width || !height || (width & 15u) || (height & 7u))
        return fail(usb, "MI8808 JPEG frame dimensions must be positive and aligned to 16 x 8");
    if (claim_output(usb) < 0 || wait_ready(usb) < 0)
        return -1;
    uint8_t dimensions[4] = {(uint8_t)width, (uint8_t)(width >> 8),
                             (uint8_t)height, (uint8_t)(height >> 8)};
    /* The supplied Mac driver sends this identical request twice. */
    for (unsigned i = 0; i < 2; ++i)
        if (control(usb, LIBUSB_ENDPOINT_OUT, 0x81, 0, dimensions, 4) < 0)
            return -1;
    return wait_ready(usb);
}

static void put16(uint8_t *p, uint16_t value)
{
    p[0] = (uint8_t)value;
    p[1] = (uint8_t)(value >> 8);
}

static void put32(uint8_t *p, uint32_t value)
{
    for (unsigned i = 0; i < 4; ++i)
        p[i] = (uint8_t)(value >> (i * 8));
}

int racer_usb_pack_header(uint8_t header[RACER_HEADER_SIZE], size_t payload_length,
                          uint32_t width, uint32_t height, uint32_t x, uint32_t y)
{
    if (!header || payload_length > UINT32_MAX - RACER_HEADER_SIZE)
        return -1;
    put16(header + 0, 0x5858);
    put32(header + 2, (uint32_t)payload_length + RACER_HEADER_SIZE);
    put16(header + 6, 0x8585);
    put32(header + 8, (uint32_t)payload_length);
    put16(header + 12, 0xdec0);
    put32(header + 14, width);
    put32(header + 18, height);
    put32(header + 22, x);
    put32(header + 26, y);
    put16(header + 30, 0xedfe);
    return 0;
}

int racer_usb_send_jpeg(struct racer_usb *usb, const uint8_t *jpeg,
                        size_t jpeg_length, uint32_t width, uint32_t height,
                        uint32_t x, uint32_t y)
{
    if (!jpeg || jpeg_length < 4 || jpeg[0] != 0xff || jpeg[1] != 0xd8 ||
        jpeg[jpeg_length - 2] != 0xff || jpeg[jpeg_length - 1] != 0xd9)
        return fail(usb, "Expected a complete baseline JPEG (SOI through EOI)");
    if (jpeg_length > INT_MAX - RACER_HEADER_SIZE || !width || !height ||
        (width & 15u) || (height & 7u) || x > UINT32_MAX - width ||
        y > UINT32_MAX - height)
        return fail(usb, "Invalid frame dimensions or JPEG length");
    if (claim_output(usb) < 0)
        return -1;
    size_t total = jpeg_length + RACER_HEADER_SIZE;
    uint8_t *packet = malloc(total);
    if (!packet)
        return fail(usb, "Allocate %zu-byte USB frame", total);
    racer_usb_pack_header(packet, jpeg_length, width, height, x, y);
    memcpy(packet + RACER_HEADER_SIZE, jpeg, jpeg_length);
    int transferred = 0;
    int result = libusb_bulk_transfer(usb->handle, RACER_ENDPOINT_OUT,
                                      packet, (int)total, &transferred,
                                      BULK_TIMEOUT_MS);
    free(packet);
    if (result < 0)
        return fail(usb, "Bulk frame: %s (%d of %zu bytes transferred)",
                    libusb_error_name(result), transferred, total);
    if (transferred != (int)total)
        return fail(usb, "Partial USB frame: %d of %zu bytes; reconnect before retrying",
                    transferred, total);
    return 0;
}
