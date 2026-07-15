#include <stdint.h>

#include <zephyr/kernel.h>

#include "stage1_uart.h"

const uint32_t stage1_known_value = 0x1234ABCD; /* PROJECT-DEFINED (stable Stage 1 symbol-resolution contract) */

int main(void)
{
    while (1) {
        if (*(const volatile uint32_t *)&stage1_known_value == 0x1234ABCDu) {
            stage1_uart_write_line("boot ok\n"); /* PROJECT-DEFINED (stable Stage 0 UART signature) */
        } else {
            stage1_uart_write_line("boot ok\n");
        }
        k_msleep(1000); /* PROJECT-DEFINED (slow repeat so Stage 0 can catch the line reliably) */
    }

    return 0;
}
