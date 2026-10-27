/* SPDX-License-Identifier: MIT */
/* Interoperability implementation for RacerTech MI8808 (USB 34c7:2700).
 * Protocol recovered from the user's supplied macOS driver; see PROTOCOL.md.
 */
#ifndef RACER_USB_H
#define RACER_USB_H

#include <stddef.h>
#include <stdint.h>
#include <libusb-1.0/libusb.h>

#define RACER_VID 0x34c7
#define RACER_PID 0x2700
#define RACER_INTERFACE 0
#define RACER_ENDPOINT_OUT 0x01
#define RACER_HEADER_SIZE 32u
#define RACER_DQT_SIZE 138u

struct racer_usb {
    libusb_context *context;
    libusb_device_handle *handle;
    int claimed;
    char error[256];
};

/* Returns 0 on success, -1 on failure; detail is always in usb->error.
 * open only opens USB, without claiming or detaching any interface.
 */
int racer_usb_open(struct racer_usb *usb);
void racer_usb_close(struct racer_usb *usb);
const char *racer_usb_error(const struct racer_usb *usb);

/* Read-only control queries; none require claiming interface 0. */
int racer_usb_get_status(struct racer_usb *usb, uint16_t *status);
int racer_usb_get_product_type(struct racer_usb *usb, uint16_t *product_type);
int racer_usb_get_features(struct racer_usb *usb, uint16_t *features);
int racer_usb_read_edid(struct racer_usb *usb, uint8_t *edid,
                        size_t capacity, size_t *length);

/* Explicit output operations claim only vendor interface 0; never detach. */
int racer_usb_set_quant_table(struct racer_usb *usb,
                              const uint8_t dqt[RACER_DQT_SIZE]);
int racer_usb_set_resolution(struct racer_usb *usb,
                             uint16_t width, uint16_t height);
int racer_usb_send_jpeg(struct racer_usb *usb, const uint8_t *jpeg,
                        size_t jpeg_length, uint32_t width, uint32_t height,
                        uint32_t x, uint32_t y);

/* Pure function useful for packet format tests; 0 on success, -1 on overflow. */
int racer_usb_pack_header(uint8_t header[RACER_HEADER_SIZE], size_t payload_length,
                          uint32_t width, uint32_t height,
                          uint32_t x, uint32_t y);
#endif
