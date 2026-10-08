#ifndef FIRMWARE_H
#define FIRMWARE_H
#include <stdint.h>
#include "build_config.h"
#ifdef __cplusplus
extern "C" {
#endif
extern volatile uint32_t firmware_heartbeat;
uint32_t advance_heartbeat(uint32_t current);
void firmware_main(void);
#ifdef __cplusplus
}
static_assert(sizeof(void*) == 4 && sizeof(long) == 4, "ARM ILP32 ABI required");
#else
_Static_assert(sizeof(void*) == 4 && sizeof(long) == 4, "ARM ILP32 ABI required");
#endif
#endif
