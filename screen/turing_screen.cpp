// Draws the Clawdmeter usage screen (the real firmware ui.cpp, landscape
// 480x320) on a Turing / TURZX 3.5" USB smart screen (VID:PID 1A86:5722).
// Data: the snapshot the daemon already writes for Quota Float. No API calls.
//
// Pixels go straight to the bulk-OUT endpoint with libusb: on macOS the
// /dev/cu.usbmodem serial driver garbles complex frames (see the
// usb-smart-screen-macos skill). Protocol: turing-smart-screen-python, rev A.
//
//   turing_screen [--orientation 2|3] [--brightness 0-100] [--snapshot PATH]
//   turing_screen --once out.rgb565 [--snapshot PATH]   (render only, no device)
#include <lvgl.h>
#include <ArduinoJson.h>
#include <libusb.h>
#include <csignal>
#include <ctime>
#include <string>
#include <unistd.h>
#include "ui.h"
#include "theme.h"
#include "splash.h"
#include "payload.h"
#include "hal/board_caps.h"

static const int W = 480, H = 320;
static uint16_t fb[W * H], sent[W * H], b1[W * 20], b2[W * 20];

// ---- Firmware glue the UI expects -----------------------------------------
static const BoardCaps caps = {"turing35", W, H, 1, false, false, false,
                                   /*no_printer*/ true, /*mac_disk_only*/ true};
const BoardCaps& board_caps(void) { return caps; }
void display_hal_set_brightness(uint8_t) {}
void ble_clear_bonds(void) {}
static lv_obj_t* sp_root = nullptr;
void splash_init(lv_obj_t* p) { sp_root = lv_obj_create(p); lv_obj_add_flag(sp_root, LV_OBJ_FLAG_HIDDEN); }
void splash_show(void) {}
void splash_hide(void) {}
lv_obj_t* splash_get_root(void) { return sp_root; }

static uint32_t now_ms() {
    timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}
static void flush(lv_display_t* d, const lv_area_t* a, uint8_t* px) {
    uint16_t* s = (uint16_t*)px;
    int w = a->x2 - a->x1 + 1;
    for (int y = a->y1; y <= a->y2; y++) { memcpy(&fb[y * W + a->x1], s, w * 2); s += w; }
    lv_display_flush_ready(d);
}

// ---- Snapshot ---------------------------------------------------------------
static std::string snapshot_path() {
    if (const char* p = getenv("TURZX_SNAPSHOT")) if (*p) return p;   // same override as host/collector.py
    return std::string(getenv("HOME")) + "/Library/Application Support/QuotaFloat/snapshot.json";
}
// Returns true and fills *out when the snapshot is new (updated_at changed)
// and fresh (< 15 s old). Older data is left to the UI's own "No link" timer.
static bool read_snapshot(const std::string& path, double* last, UsageData* out) {
    FILE* f = fopen(path.c_str(), "rb");
    if (!f) return false;
    char buf[4096]; size_t n = fread(buf, 1, sizeof buf - 1, f); fclose(f); buf[n] = 0;
    JsonDocument doc;
    if (deserializeJson(doc, buf)) return false;
    double at = doc["updated_at"] | 0.0;
    const double age = time(nullptr) - at;   // a future stamp is as untrustworthy as an old one
    if (at == *last || age > 15 || age < -5) return false;
    std::string payload; serializeJson(doc["payload"], payload);
    UsageData d; memset(&d, 0, sizeof d);
    if (!parse_usage_json(payload.c_str(), &d)) return false;
    *last = at; *out = d;
    return true;
}

// ---- Turing rev A over libusb -------------------------------------------------
static libusb_device_handle* dev = nullptr;
static int orientation = 2, brightness = 70;   // 2 = landscape, 3 = reverse landscape

static void pack(uint8_t* b, int c, int x, int y, int ex, int ey) {
    b[0] = x >> 2; b[1] = ((x & 3) << 6) + (y >> 4); b[2] = ((y & 15) << 4) + (ex >> 6);
    b[3] = ((ex & 63) << 2) + (ey >> 8); b[4] = ey & 255; b[5] = c;
}
static bool usb_write(const void* data, int len) {
    const uint8_t* p = (const uint8_t*)data;
    while (len > 0) {
        int chunk = len > 16384 ? 16384 : len, done = 0;
        if (libusb_bulk_transfer(dev, 0x03, (uint8_t*)p, chunk, &done, 8000) != 0) return false;
        p += done; len -= done;
    }
    return true;
}
static bool cmd(int c, int x = 0, int y = 0, int ex = 0, int ey = 0) {
    uint8_t b[6]; pack(b, c, x, y, ex, ey); return usb_write(b, 6);
}
static void usb_close() {
    if (!dev) return;
    libusb_release_interface(dev, 1); libusb_close(dev); dev = nullptr;
}
static bool usb_open() {
    dev = libusb_open_device_with_vid_pid(nullptr, 0x1A86, 0x5722);
    if (!dev) return false;
    libusb_detach_kernel_driver(dev, 1);   // macOS says "access denied"; claiming still works
    if (libusb_claim_interface(dev, 1) != 0) { usb_close(); return false; }
    uint8_t o[16] = {0}; pack(o, 121, 0, 0, 0, 0);
    o[6] = orientation + 100; o[7] = W >> 8; o[8] = W & 255; o[9] = H >> 8; o[10] = H & 255;
    bool ok = cmd(109) && cmd(110, 255 - brightness * 255 / 100) && usb_write(o, 16);
    // ponytail: no software recovery known. Once a previous process has let go,
    // this unit fails every bulk write (clear_halt / alt setting / CDC line state
    // don't help; libusb_reset_device drops it off the bus). Only a replug clears it.
    static bool warned = false;
    if (!ok && !warned) { warned = true; fprintf(stderr, "screen found but not accepting data: unplug and replug it\n"); }
    if (ok) warned = false;
    if (!ok) usb_close();
    return ok;
}
// Sends the rows that changed since the last frame (full width, one rectangle).
static bool push_frame(bool full) {
    int y0 = 0, y1 = H - 1;
    if (!full) {
        while (y0 < H && !memcmp(&fb[y0 * W], &sent[y0 * W], W * 2)) y0++;
        if (y0 == H) return true;
        while (!memcmp(&fb[y1 * W], &sent[y1 * W], W * 2)) y1--;
    }
    if (!cmd(197, 0, y0, W - 1, y1) || !usb_write(&fb[y0 * W], (y1 - y0 + 1) * W * 2)) return false;
    memcpy(&sent[y0 * W], &fb[y0 * W], (y1 - y0 + 1) * W * 2);
    return true;
}

static volatile sig_atomic_t stop = 0;

int main(int ac, char** av) {
    const char* once = nullptr;
    std::string snap = snapshot_path();
    for (int i = 1; i + 1 < ac; i += 2) {
        if (!strcmp(av[i], "--once")) once = av[i + 1];
        else if (!strcmp(av[i], "--orientation")) orientation = atoi(av[i + 1]) == 3 ? 3 : 2;
        else if (!strcmp(av[i], "--brightness")) brightness = LV_CLAMP(0, atoi(av[i + 1]), 100);
        else if (!strcmp(av[i], "--snapshot")) snap = av[i + 1];
    }

    lv_init();
    lv_tick_set_cb(now_ms);
    lv_display_t* disp = lv_display_create(W, H);
    lv_display_set_color_format(disp, LV_COLOR_FORMAT_RGB565);   // little-endian, as the screen wants
    lv_display_set_flush_cb(disp, flush);
    lv_display_set_buffers(disp, b1, b2, sizeof b1, LV_DISPLAY_RENDER_MODE_PARTIAL);
    theme_apply();
    ui_init();
    ui_update_battery(-1, false);
    ui_set_data_source(DATA_SRC_USB);
    ui_show_screen(SCREEN_USAGE);

    double last = 0;
    UsageData d;
    if (once) {
        if (read_snapshot(snap, &last, &d)) ui_update(&d);
        else fprintf(stderr, "snapshot missing or older than 15 s: drawing the no-data state\n");
        for (int i = 0; i < 300; i++) { lv_timer_handler(); ui_tick_anim(); usleep(2000); }
        lv_obj_invalidate(lv_screen_active()); lv_refr_now(nullptr);
        FILE* f = fopen(once, "wb");
        if (!f) { perror(once); return 1; }
        fwrite(fb, 2, W * H, f); fclose(f);
        return 0;
    }

    signal(SIGTERM, [](int) { stop = 1; });
    signal(SIGINT, [](int) { stop = 1; });
    libusb_init(nullptr);
    uint32_t next_snap = 0, next_try = 0;
    bool need_full = true;
    while (!stop) {
        uint32_t t = now_ms();
        if ((int32_t)(t - next_snap) >= 0) {
            next_snap = t + 1000;
            if (read_snapshot(snap, &last, &d)) ui_update(&d);
        }
        lv_timer_handler();
        ui_tick_anim();
        if (!dev && (int32_t)(t - next_try) >= 0) {
            next_try = t + 5000;
            if (usb_open()) { need_full = true; fprintf(stderr, "screen connected\n"); }
        }
        if (dev) {
            if (push_frame(need_full)) need_full = false;
            else { fprintf(stderr, "screen write failed; reconnecting\n"); usb_close(); }
        }
        usleep(100 * 1000);   // ~10 fps is plenty for a status screen
    }
    usb_close();
    libusb_exit(nullptr);
    return 0;
}
