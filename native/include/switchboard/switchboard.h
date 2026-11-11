#pragma once

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#if defined(SWITCHBOARD_NATIVE_BUILD)
#define SB_API __declspec(dllexport)
#else
#define SB_API __declspec(dllimport)
#endif
#else
#define SB_API __attribute__((visibility("default")))
#endif

#if defined(__cplusplus)
extern "C"
{
#endif

#define SB_ABI_VERSION 1U
#define SB_MAX_INPUT_BYTES (1024U * 1024U)

    typedef struct sb_runtime sb_runtime;

    typedef struct sb_buffer
    {
        uint8_t *data;
        size_t length;
    } sb_buffer;

    typedef int32_t sb_status;

    enum
    {
        SB_OK = 0,
        SB_INVALID_ARGUMENT = 1,
        SB_RUNTIME_ERROR = 2,
        SB_ALLOCATION_ERROR = 3
    };

    typedef int32_t sb_origin;

    enum
    {
        SB_ASSISTANT = 0,
        SB_RULE = 1,
        SB_HUMAN = 2
    };

    SB_API uint32_t sb_abi_version(void);
    SB_API sb_status sb_runtime_create(const uint8_t *configuration, size_t length, sb_runtime **output, sb_buffer *error);
    SB_API void sb_runtime_destroy(sb_runtime *runtime);
    SB_API sb_status sb_runtime_catalog(sb_runtime *runtime, sb_buffer *output);
    SB_API sb_status sb_runtime_call(sb_runtime *runtime, const uint8_t *request, size_t length, sb_origin origin, sb_buffer *output);
    SB_API sb_status sb_runtime_approve(sb_runtime *runtime, const uint8_t *id, size_t length, sb_buffer *output);
    SB_API sb_status sb_runtime_deny(sb_runtime *runtime, const uint8_t *id, size_t length, sb_buffer *output);
    SB_API sb_status sb_runtime_tick(sb_runtime *runtime, sb_buffer *output);
    SB_API sb_status sb_runtime_rpc(sb_runtime *runtime, const uint8_t *request, size_t length, sb_buffer *output);
    SB_API void sb_buffer_free(sb_buffer *buffer);

#if defined(__cplusplus)
}
#endif
