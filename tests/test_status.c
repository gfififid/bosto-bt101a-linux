#include "racer_usb.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static int reply_length;
static uint8_t reply_low, reply_high;

int __wrap_libusb_control_transfer(libusb_device_handle *handle,
    uint8_t type, uint8_t request, uint16_t value, uint16_t index,
    unsigned char *data, uint16_t length, unsigned int timeout)
{
    assert(handle && type == 0xc1 && request == 0x40 && value == 0 && index == 0);
    assert(length == 2 && timeout > 0);
    if (reply_length > 0) data[0] = reply_low;
    if (reply_length > 1) data[1] = reply_high;
    return reply_length;
}

int main(void)
{
    static char fake_handle;
    struct racer_usb usb = {.handle = (libusb_device_handle *)&fake_handle};
    uint16_t status = 0xffff;
    reply_length = 1; reply_low = 0; reply_high = 0xff;
    assert(racer_usb_get_status(&usb, &status) == 0 && status == 0);
    reply_low = 0x85;
    assert(racer_usb_get_status(&usb, &status) == 0 && status == 0x85);
    reply_length = 2; reply_low = 0x12; reply_high = 0x34;
    assert(racer_usb_get_status(&usb, &status) == 0 && status == 0x3412);
    reply_length = 0;
    assert(racer_usb_get_status(&usb, &status) == -1);
    reply_length = LIBUSB_ERROR_TIMEOUT;
    assert(racer_usb_get_status(&usb, &status) == -1);
    assert(strstr(racer_usb_error(&usb), "TIMEOUT"));
    assert(racer_usb_get_status(&usb, NULL) == -1);
    usb.handle = NULL;
    assert(racer_usb_get_status(&usb, &status) == -1);
    puts("USB status: one/two-byte replies, short/failed transfer checks passed");
    return 0;
}
