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
#define SB_ABI_REVISION 2U
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

    /* Revision 2: typed outcomes. status is the JSON outcome status; effect is derived as specified in
       contracts/README.md. SB_EFFECT_CONFIRMED means matching software readback, never physical proof. */
    enum
    {
        SB_OUTCOME_OK = 0,
        SB_OUTCOME_PENDING = 1,
        SB_OUTCOME_DENIED = 2,
        SB_OUTCOME_INVALID = 3,
        SB_OUTCOME_ERROR = 4
    };

    enum
    {
        SB_EFFECT_NONE = 0,
        SB_EFFECT_RETURNED = 1,
        SB_EFFECT_CONFIRMED = 2,
        SB_EFFECT_UNCONFIRMED = 3,
        SB_EFFECT_UNKNOWN = 4,
        SB_EFFECT_FAILED = 5
    };

    typedef struct sb_outcome
    {
        int32_t status;
        int32_t effect;
        char approval_id[33];
        char action_id[33];
    } sb_outcome;

    SB_API uint32_t sb_abi_version(void);
    SB_API uint32_t sb_abi_revision(void);
    SB_API sb_status sb_runtime_create(const uint8_t *configuration, size_t length, sb_runtime **output, sb_buffer *error);
    SB_API void sb_runtime_destroy(sb_runtime *runtime);
    SB_API sb_status sb_runtime_catalog(sb_runtime *runtime, sb_buffer *output);
    SB_API sb_status sb_runtime_call(sb_runtime *runtime, const uint8_t *request, size_t length, sb_origin origin, sb_buffer *output);
    SB_API sb_status sb_runtime_call_outcome(sb_runtime *runtime, const uint8_t *request, size_t length, sb_origin origin, sb_outcome *outcome,
                                             sb_buffer *report);
    /* Shows one approval with its exact arguments so an operator can review it before approving. */
    SB_API sb_status sb_runtime_approval(sb_runtime *runtime, const uint8_t *id, size_t length, sb_buffer *output);
    SB_API sb_status sb_runtime_approve(sb_runtime *runtime, const uint8_t *id, size_t length, sb_buffer *output);
    SB_API sb_status sb_runtime_deny(sb_runtime *runtime, const uint8_t *id, size_t length, sb_buffer *output);
    SB_API sb_status sb_runtime_tick(sb_runtime *runtime, sb_buffer *output);
    SB_API sb_status sb_runtime_rpc(sb_runtime *runtime, const uint8_t *request, size_t length, sb_buffer *output);
    /* Operator interface. actions filter: {"state"?, "limit"?} or {"id"}; reconcile: {"id","resolution","note"}. */
    SB_API sb_status sb_runtime_actions(sb_runtime *runtime, const uint8_t *filter, size_t length, sb_buffer *output);
    SB_API sb_status sb_runtime_reconcile(sb_runtime *runtime, const uint8_t *request, size_t length, sb_buffer *output);
    /* mode: -1 reports, 0 turns off, 1 turns on. */
    SB_API sb_status sb_runtime_kill_switch(sb_runtime *runtime, int32_t mode, sb_buffer *output);
    SB_API sb_status sb_runtime_backup(sb_runtime *runtime, const uint8_t *path, size_t length, sb_buffer *output);
    SB_API sb_status sb_runtime_purge(sb_runtime *runtime, int32_t dry_run, sb_buffer *output);
    SB_API sb_status sb_verify_backup(const uint8_t *path, size_t length, sb_buffer *output);
    /* Thread-safe, lock-free cancellation of web transfers in the call currently running on runtime. */
    SB_API void sb_runtime_cancel(sb_runtime *runtime);
    SB_API void sb_buffer_free(sb_buffer *buffer);

#if defined(__cplusplus)
}
#endif
