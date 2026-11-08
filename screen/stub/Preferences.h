#pragma once
#include <stdint.h>
class Preferences { public:
 bool begin(const char*,bool){return false;} void end(){}
 bool putBool(const char*,bool){return true;} bool putUChar(const char*,uint8_t){return true;}
 bool getBool(const char*,bool d){return d;} uint8_t getUChar(const char*,uint8_t d){return d;} };
