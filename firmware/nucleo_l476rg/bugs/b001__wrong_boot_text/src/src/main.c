#include <stdint.h>

#include <zephyr/kernel.h>

#include "stage1_uart.h"

const uint32_t stage1_known_value = 0x1234ABCD; /* BENCHMARK-DEFINED (bug case keeps the symbol healthy) */

int main(void)
{
    while (1) {
        if (*(const volatile uint32_t *)&stage1_known_value == 0x1234ABCDu) {
            stage1_uart_write_line("boot nope\n"); /* BENCHMARK-DEFINED (intentional wrong UART signature) */
        } else {
            stage1_uart_write_line("boot nope\n");
        }
        k_msleep(1000);
    }

    return 0;
}
