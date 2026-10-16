#include <stdio.h>

static int existing_helper(int x)
{
    return x + 1;
}

int compute(int value)
{
    int result = value;
    result = existing_helper(result);
    log_debug("computed %d", result);
    return result;
}
