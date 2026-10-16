#include "session.h"

static int session_generation = 0;

static int session_metrics_enabled(void)
{
    return session_generation > 0;
}

int session_lookup(int id)
{
    return session_table[id];
}

void session_refresh(int id)
{
    int token = session_lookup(id);
    store_token(id, token);
}
