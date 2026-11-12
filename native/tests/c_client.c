#include "switchboard/switchboard.h"

#include <stdint.h>
#include <string.h>

/* Typed pointers pin each revision 2 signature at compile time; calls with no runtime need no files. */
static int revision_two(void)
{
    sb_status (*call_outcome)(sb_runtime *, const uint8_t *, size_t, sb_origin, sb_outcome *, sb_buffer *) = sb_runtime_call_outcome;
    sb_status (*approval)(sb_runtime *, const uint8_t *, size_t, sb_buffer *) = sb_runtime_approval;
    sb_status (*actions)(sb_runtime *, const uint8_t *, size_t, sb_buffer *) = sb_runtime_actions;
    sb_status (*reconcile)(sb_runtime *, const uint8_t *, size_t, sb_buffer *) = sb_runtime_reconcile;
    sb_status (*kill_switch)(sb_runtime *, int32_t, sb_buffer *) = sb_runtime_kill_switch;
    sb_status (*backup)(sb_runtime *, const uint8_t *, size_t, sb_buffer *) = sb_runtime_backup;
    sb_status (*purge)(sb_runtime *, int32_t, sb_buffer *) = sb_runtime_purge;
    sb_status (*verify)(const uint8_t *, size_t, sb_buffer *) = sb_verify_backup;
    void (*cancel)(sb_runtime *) = sb_runtime_cancel;
    sb_buffer output = {0, 0};
    sb_outcome outcome;
    int failed = 0;
    if (SB_ABI_REVISION < 2U || sb_abi_revision() != SB_ABI_REVISION || sizeof(outcome.approval_id) != 33 || sizeof(outcome.action_id) != 33)
    {
        return 1;
    }
    if (SB_OUTCOME_OK != 0 || SB_OUTCOME_PENDING != 1 || SB_OUTCOME_DENIED != 2 || SB_OUTCOME_INVALID != 3 || SB_OUTCOME_ERROR != 4)
    {
        return 1;
    }
    if (SB_EFFECT_NONE != 0 || SB_EFFECT_RETURNED != 1 || SB_EFFECT_CONFIRMED != 2 || SB_EFFECT_UNCONFIRMED != 3 || SB_EFFECT_UNKNOWN != 4 ||
        SB_EFFECT_FAILED != 5)
    {
        return 1;
    }
    memset(&outcome, 0x7f, sizeof(outcome));
    failed = call_outcome(0, 0, 0, SB_ASSISTANT, &outcome, &output) != SB_INVALID_ARGUMENT || outcome.status != SB_OUTCOME_ERROR ||
             outcome.effect != SB_EFFECT_NONE || outcome.approval_id[0] != '\0' || outcome.action_id[0] != '\0';
    sb_buffer_free(&output);
    failed = failed || call_outcome(0, 0, 0, SB_ASSISTANT, 0, &output) != SB_INVALID_ARGUMENT;
    sb_buffer_free(&output);
    failed = failed || approval(0, (const uint8_t *)"x", 1, &output) != SB_INVALID_ARGUMENT;
    sb_buffer_free(&output);
    failed = failed || actions(0, 0, 0, &output) != SB_INVALID_ARGUMENT;
    sb_buffer_free(&output);
    failed = failed || reconcile(0, 0, 0, &output) != SB_INVALID_ARGUMENT;
    sb_buffer_free(&output);
    failed = failed || kill_switch(0, -1, &output) != SB_INVALID_ARGUMENT;
    sb_buffer_free(&output);
    failed = failed || kill_switch(0, 7, &output) != SB_INVALID_ARGUMENT;
    sb_buffer_free(&output);
    failed = failed || backup(0, 0, 0, &output) != SB_INVALID_ARGUMENT;
    sb_buffer_free(&output);
    failed = failed || purge(0, 1, &output) != SB_INVALID_ARGUMENT;
    sb_buffer_free(&output);
    failed = failed || verify(0, 1, &output) != SB_INVALID_ARGUMENT;
    sb_buffer_free(&output);
    cancel(0);
    return failed || output.data != 0 || output.length != 0;
}

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
    if (output.data != 0 || output.length != 0)
    {
        return 1;
    }
    return revision_two();
}
