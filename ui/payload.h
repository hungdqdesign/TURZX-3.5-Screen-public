#pragma once
#include "data.h"

// Parses one daemon JSON payload. false = not valid JSON (out untouched).
bool parse_usage_json(const char* json, UsageData* out);
