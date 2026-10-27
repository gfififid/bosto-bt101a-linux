# CC may be overridden, e.g. make CC=clang. Avoid sandboxed ccache paths.
CC = /usr/bin/gcc
PKG_CONFIG ?= pkg-config
CFLAGS ?= -O2 -g
WARN = -std=c11 -Wall -Wextra -Werror
CPPFLAGS += -Isrc -Ivendor/evdi/library $(shell $(PKG_CONFIG) --cflags libusb-1.0 libjpeg libdrm)
LIBS = $(shell $(PKG_CONFIG) --libs libusb-1.0 libjpeg)
SRC = src/main.c src/jpeg_frame.c src/racer_usb.c src/frame_region.c
comma := ,
STREAM_WRAPS = clock_gettime poll evdi_open_attached_to_fixed evdi_enable_cursor_events evdi_connect2 evdi_get_event_ready evdi_handle_events evdi_register_buffer evdi_unregister_buffer evdi_request_update evdi_grab_pixels evdi_disconnect evdi_close bosto_encode_jpeg bosto_extract_dqt racer_usb_set_quant_table racer_usb_set_resolution racer_usb_send_jpeg

.PHONY: all module test clean
all: build/bosto-display

build:
	mkdir -p $@

build/libevdi.so.1: vendor/evdi/library/evdi_lib.c vendor/evdi/library/evdi_procfs.c | build
	$(MAKE) -C vendor/evdi/library CC="$(CC)"
	cp vendor/evdi/library/libevdi.so.1.15.1 $@
	ln -sf libevdi.so.1 build/libevdi.so

build/bosto-display: $(SRC) src/jpeg_frame.h src/racer_usb.h src/frame_region.h build/libevdi.so.1
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARN) $(SRC) -Lbuild -Wl,-rpath,'$$ORIGIN' -levdi $(LIBS) -o $@

module: | build
	$(MAKE) -C vendor/evdi/module CC="$(CC)"
	cp vendor/evdi/module/evdi.ko build/evdi.ko

build/test_packet: tests/test_packet.c src/racer_usb.c src/racer_usb.h | build
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARN) tests/test_packet.c src/racer_usb.c $(shell $(PKG_CONFIG) --libs libusb-1.0) -o $@

build/test_jpeg: tests/test_jpeg.c src/jpeg_frame.c src/jpeg_frame.h | build
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARN) -DBOSTO_TEST_JPEG_FAILURE tests/test_jpeg.c src/jpeg_frame.c -Wl,--wrap=jpeg_write_scanlines $(shell $(PKG_CONFIG) --libs libjpeg) -o $@

build/test_status: tests/test_status.c src/racer_usb.c src/racer_usb.h | build
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARN) tests/test_status.c src/racer_usb.c -Wl,--wrap=libusb_control_transfer $(shell $(PKG_CONFIG) --libs libusb-1.0) -o $@

build/test_region: tests/test_region.c src/frame_region.c src/frame_region.h | build
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARN) tests/test_region.c src/frame_region.c -o $@

build/test_stream: tests/test_stream.c $(SRC) build/libevdi.so.1
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARN) tests/test_stream.c src/jpeg_frame.c src/racer_usb.c src/frame_region.c -Lbuild -Wl,-rpath,'$$ORIGIN' -levdi $(LIBS) $(foreach symbol,$(STREAM_WRAPS),-Wl$(comma)--wrap=$(symbol)) -o $@

test: all build/test_packet build/test_jpeg build/test_status build/test_region build/test_stream
	./build/test_packet
	./build/test_jpeg
	./build/test_status
	./build/test_region
	./build/test_stream >build/stream-test.log 2>&1
	tail -5 build/stream-test.log
	python3 -m py_compile tools/diagnose.py
	python3 -m unittest discover -s tests -p 'test_inspect_pen.py'
	python3 scripts/test_install.py
	./build/bosto-display --help >/dev/null
	./build/bosto-display --offline-pattern build/test-pattern.jpg

clean:
	rm -rf build
	$(MAKE) -C vendor/evdi/library clean
	$(MAKE) -C vendor/evdi/module clean
