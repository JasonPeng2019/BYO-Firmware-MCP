#include "firmware.h"
volatile uint32_t firmware_heartbeat;
void firmware_main(void) {
    for (;;) { firmware_heartbeat = advance_heartbeat(firmware_heartbeat); }
}
