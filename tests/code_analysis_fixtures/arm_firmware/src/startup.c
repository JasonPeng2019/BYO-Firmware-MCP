#include "firmware.h"
extern uint32_t _stack_top, _data_load, _data_start, _data_end, _bss_start, _bss_end;
void reset_handler(void);
void default_handler(void) { for (;;) {} }
__attribute__((used,section(".isr_vector")))
const uintptr_t vectors[] = { (uintptr_t)&_stack_top, (uintptr_t)reset_handler,
    (uintptr_t)default_handler, (uintptr_t)default_handler };
void reset_handler(void) {
    uintptr_t source = (uintptr_t)&_data_load;
    for (uintptr_t dest = (uintptr_t)&_data_start; dest < (uintptr_t)&_data_end; dest += sizeof(uint32_t)) {
        *(uint32_t*)dest = *(const uint32_t*)source; source += sizeof(uint32_t);
    }
    for (uintptr_t dest = (uintptr_t)&_bss_start; dest < (uintptr_t)&_bss_end; dest += sizeof(uint32_t)) { *(uint32_t*)dest = 0; }
    firmware_main();
}
