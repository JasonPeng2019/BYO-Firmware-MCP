/* Temporary HIL test image: reset-loop only, no peripheral side effects. */
#include <stdint.h>

extern uint32_t _estack;
void Reset_Handler(void);

__attribute__((section(".isr_vector"), used))
void (* const vectors[])(void) = {
    (void (*)(void))(&_estack),
    Reset_Handler,
};

void Reset_Handler(void) {
    for (;;) {
        __asm volatile ("nop");
    }
}
