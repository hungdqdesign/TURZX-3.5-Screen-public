// The usage JSON payload -> UsageData. Used by the firmware and by
// tools/turing_screen (the same UI drawn on a USB smart screen).
#include <ArduinoJson.h>
#include <math.h>
#include <string.h>
#include "payload.h"

bool parse_usage_json(const char* json, UsageData* out) {
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, json);
    if (err) return false;

    out->session_pct = doc["s"] | -1.0f;
    out->session_reset_mins = doc["sr"] | -1;
    out->weekly_pct = doc["w"] | -1.0f;
    out->weekly_reset_mins = doc["wr"] | -1;
    out->fable_pct = doc["f"] | -1.0f;
    out->fable_reset_mins = doc["fr"] | -1;
    out->codex_pct = doc["c"] | -1.0f;      // absent/-1 = daemon had no Codex data
    out->codex_reset_mins = doc["cr"] | -1;
    out->codex_mode = (doc["cm"] | 0) == 1 && isfinite(out->codex_pct) && out->codex_pct >= 100.0f ? 1 : 0;
    out->codex_credits = doc["cc"] | -1.0;
    if (!isfinite(out->codex_credits) || out->codex_credits < 0) out->codex_credits = -1;
    out->codex_credit_stale = (doc["cs"] | 0) == 1;
    out->antigravity_pct = doc["a"] | -1.0f;
    out->antigravity_reset_mins = doc["ar"] | -1;
    out->antigravity_bucket = doc["ab"] | -1;
    // An old daemon's CPU number must never become an AntiGravity quota.
    if (!isfinite(out->antigravity_pct) || out->antigravity_pct < 0.0f ||
        out->antigravity_pct > 100.0f || out->antigravity_bucket < 0 ||
        out->antigravity_bucket > 5) {
        out->antigravity_pct = -1.0f;
        out->antigravity_reset_mins = -1;
        out->antigravity_bucket = -1;
    }
    // Host Mac load; absent (older daemon) → -1 → the gauges read --%.
    out->cpu_pct = doc["cpu"] | -1.0f;
    out->gpu_pct = doc["gpu"] | -1.0f;
    out->ram_pct = doc["ram"] | -1.0f;
    out->disk_pct = doc["dsk"] | -1.0f;
    out->ram_used_gb = doc["ru"] | -1.0f;
    out->ram_total_gb = doc["rt"] | -1;
    out->disk_free_gb = doc["df"] | -1;
    out->ssd_pct = doc["sp"] | -1.0f;
    out->ssd_free_gb = doc["sf"] | -1;
    out->hdd_pct = doc["hp"] | -1.0f;
    out->hdd_free_gb = doc["hf"] | -1;
    out->cpu_cores = doc["nc"] | -1;
    out->gpu_cores = doc["ng"] | -1;
    // 3D printer; absent (printer off, older daemon, BLE) → -1 → column reads "No data".
    out->print_state = doc["ps"] | -1;
    out->print_pct = doc["pp"] | -1;
    out->print_mins_left = doc["pm"] | -1;
    out->print_layer = doc["pl"] | -1;
    out->print_layers = doc["pL"] | -1;
    out->print_stall_mins = doc["pq"] | -1;
    strlcpy(out->print_reason, doc["pr"] | "", sizeof(out->print_reason));
    strlcpy(out->status, doc["st"] | "unknown", sizeof(out->status));
    out->ok = doc["ok"] | false;
    out->time_secs = doc["t"] | -1;
    out->valid = true;
    return true;
}
