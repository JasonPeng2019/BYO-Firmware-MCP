#include "firmware.h"
template<typename T> T increment(T value) { return value + T(HEARTBEAT_STEP); }
uint32_t advance_heartbeat(uint32_t current) { return increment(current); }
