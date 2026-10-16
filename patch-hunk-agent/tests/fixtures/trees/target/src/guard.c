#include "guard.h"

static int guard_limit = 0;

void guard_init(void)
{
    guard_limit = DEFAULT_GUARD_LIMIT;
}

int guard_check(int value)
{
    return value < guard_limit;
}

int guard_reset(void)
{
    guard_limit = 0;
    return 0;
}

int guard_remaining(int value)
{
    int left = 0;
    if (value < guard_limit) {
        left = guard_limit - value;
    }
    return left;
}
