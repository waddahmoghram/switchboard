#include "switchboard/switchboard.h"

#include <stdint.h>

int main(void)
{
    sb_buffer output = {0, 0};
    if (sizeof(sb_status) != sizeof(int32_t) || sizeof(sb_origin) != sizeof(int32_t))
    {
        return 1;
    }
    if (sb_abi_version() != SB_ABI_VERSION || sb_runtime_catalog(0, &output) != SB_INVALID_ARGUMENT)
    {
        sb_buffer_free(&output);
        return 1;
    }
    sb_buffer_free(&output);
    sb_buffer_free(&output);
    return output.data == 0 && output.length == 0 ? 0 : 1;
}
