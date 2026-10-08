#include "util.h"

/* Calls scale_value twice so references are observable. */
static int twice(int value) { return scale_value(value, 2); }

int main(void)
{
    int total = scale_value(3, 4);
    return twice(total) + twice(1);
}
