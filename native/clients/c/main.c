#include "switchboard/switchboard.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char USAGE[] = "Usage: switchboard-c CONFIG.json COMMAND [ARGUMENTS]\n"
                            "       switchboard-c verify-backup DIRECTORY\n"
                            "Commands: catalog | tick | call JSON | outcome JSON | rpc JSON | approval ID | approve ID | deny ID |\n"
                            "          actions [FILTER_JSON] | reconcile ID applied|not_applied NOTE |\n"
                            "          kill-switch status|on|off | backup DIRECTORY | purge dry-run|apply\n";

static int print_result(const sb_status status, sb_buffer *output)
{
    FILE *stream = status == SB_OK ? stdout : stderr;
    if (output->data != NULL && output->length != 0)
    {
        if (fwrite(output->data, 1, output->length, stream) != output->length || fputc('\n', stream) == EOF)
        {
            fputs("ERROR: writing the native C response failed\n", stderr);
            sb_buffer_free(output);
            return 1;
        }
    }
    else if (status != SB_OK)
    {
        fprintf(stderr, "ERROR: native C operation failed with code %d\n", (int)status);
    }
    sb_buffer_free(output);
    return status == SB_OK ? 0 : 1;
}

static const char *status_name(const int32_t status)
{
    switch (status)
    {
    case SB_OUTCOME_OK:
        return "ok";
    case SB_OUTCOME_PENDING:
        return "pending";
    case SB_OUTCOME_DENIED:
        return "denied";
    case SB_OUTCOME_INVALID:
        return "invalid";
    case SB_OUTCOME_ERROR:
        return "error";
    default:
        return "unrecognized";
    }
}

static const char *effect_name(const int32_t effect)
{
    switch (effect)
    {
    case SB_EFFECT_NONE:
        return "none";
    case SB_EFFECT_RETURNED:
        return "returned";
    case SB_EFFECT_CONFIRMED:
        return "confirmed";
    case SB_EFFECT_UNCONFIRMED:
        return "unconfirmed";
    case SB_EFFECT_UNKNOWN:
        return "unknown";
    case SB_EFFECT_FAILED:
        return "failed";
    default:
        return "unrecognized";
    }
}

/* Identifiers are validated lowercase hexadecimal, so they need no JSON escaping; empty ones print as null. */
static int print_identifier(const char *identifier, const size_t capacity)
{
    if (memchr(identifier, '\0', capacity) == NULL || identifier[0] == '\0')
    {
        return fputs("null", stdout) == EOF;
    }
    return printf("\"%s\"", identifier) < 0;
}

/* Prints one JSON line: the typed fields, then the complete report the library returned. */
static int print_outcome(const sb_status status, const sb_outcome *outcome, sb_buffer *report)
{
    int failed = 0;
    if (status != SB_OK)
    {
        if (outcome->effect == SB_EFFECT_UNKNOWN)
        {
            fputs("ERROR: the native call failed after it started; its effect is unknown, so inspect actions before retrying\n", stderr);
        }
        return print_result(status, report);
    }
    failed = printf("{\"outcome\":{\"status\":\"%s\",\"effect\":\"%s\",\"approval_id\":", status_name(outcome->status), effect_name(outcome->effect)) < 0;
    failed = failed || print_identifier(outcome->approval_id, sizeof(outcome->approval_id));
    failed = failed || fputs(",\"action_id\":", stdout) == EOF;
    failed = failed || print_identifier(outcome->action_id, sizeof(outcome->action_id));
    failed = failed || fputs("},\"report\":", stdout) == EOF;
    if (report->data != NULL && report->length != 0)
    {
        failed = failed || fwrite(report->data, 1, report->length, stdout) != report->length;
    }
    else
    {
        failed = failed || fputs("null", stdout) == EOF;
    }
    failed = failed || fputs("}\n", stdout) == EOF || fflush(stdout) == EOF;
    sb_buffer_free(report);
    if (failed)
    {
        fputs("ERROR: writing the native C outcome failed\n", stderr);
        return 1;
    }
    return 0;
}

/* Appends bytes and keeps the output NUL-terminated; returns 0 when the bounded output would overflow. */
static int append_bytes(char *output, const size_t capacity, size_t *used, const char *bytes, const size_t count)
{
    if (count >= capacity - *used)
    {
        return 0;
    }
    memcpy(output + *used, bytes, count);
    *used += count;
    output[*used] = '\0';
    return 1;
}

static int append_literal(char *output, const size_t capacity, size_t *used, const char *text)
{
    return append_bytes(output, capacity, used, text, strlen(text));
}

/* Appends text as the body of a JSON string. The library rejects invalid UTF-8 rather than repairing it. */
static int append_json_text(char *output, const size_t capacity, size_t *used, const char *text)
{
    static const char digits[] = "0123456789abcdef";
    const unsigned char *cursor = (const unsigned char *)text;
    for (; *cursor != '\0'; ++cursor)
    {
        char escaped[6];
        size_t count = 1;
        escaped[0] = (char)*cursor;
        if (*cursor == '"' || *cursor == '\\')
        {
            escaped[0] = '\\';
            escaped[1] = (char)*cursor;
            count = 2;
        }
        else if (*cursor < 0x20U)
        {
            escaped[0] = '\\';
            escaped[1] = 'u';
            escaped[2] = '0';
            escaped[3] = '0';
            escaped[4] = digits[*cursor >> 4];
            escaped[5] = digits[*cursor & 0x0FU];
            count = 6;
        }
        if (!append_bytes(output, capacity, used, escaped, count))
        {
            return 0;
        }
    }
    return 1;
}

/* Builds {"id":...,"resolution":...,"note":...}; the caller frees the result. */
static char *reconcile_request(const char *id, const char *resolution, const char *note, size_t *length)
{
    const size_t id_length = strlen(id);
    const size_t resolution_length = strlen(resolution);
    const size_t note_length = strlen(note);
    size_t capacity = 0;
    size_t used = 0;
    char *request = NULL;
    if (id_length > SB_MAX_INPUT_BYTES || resolution_length > SB_MAX_INPUT_BYTES || note_length > SB_MAX_INPUT_BYTES)
    {
        return NULL;
    }
    /* Escaping expands one byte to at most six. */
    capacity = 6 * (id_length + resolution_length + note_length) + 64;
    request = malloc(capacity);
    if (request == NULL)
    {
        return NULL;
    }
    request[0] = '\0';
    if (!append_literal(request, capacity, &used, "{\"id\":\"") || !append_json_text(request, capacity, &used, id) ||
        !append_literal(request, capacity, &used, "\",\"resolution\":\"") || !append_json_text(request, capacity, &used, resolution) ||
        !append_literal(request, capacity, &used, "\",\"note\":\"") || !append_json_text(request, capacity, &used, note) ||
        !append_literal(request, capacity, &used, "\"}"))
    {
        free(request);
        return NULL;
    }
    *length = used;
    return request;
}

static int kill_switch_mode(const char *word, int32_t *mode)
{
    if (strcmp(word, "status") == 0)
    {
        *mode = -1;
    }
    else if (strcmp(word, "off") == 0)
    {
        *mode = 0;
    }
    else if (strcmp(word, "on") == 0)
    {
        *mode = 1;
    }
    else
    {
        return 0;
    }
    return 1;
}

static const uint8_t *bytes(const char *text)
{
    return (const uint8_t *)text;
}

/* words[0] is the command; count includes it. Every path frees its response buffer. */
static int run_command(sb_runtime *runtime, const int count, char **words)
{
    const char *command = words[0];
    sb_buffer response = {NULL, 0};
    sb_status status = SB_INVALID_ARGUMENT;
    if (count == 1 && strcmp(command, "catalog") == 0)
    {
        status = sb_runtime_catalog(runtime, &response);
    }
    else if (count == 1 && strcmp(command, "tick") == 0)
    {
        status = sb_runtime_tick(runtime, &response);
    }
    else if (count == 2 && strcmp(command, "call") == 0)
    {
        status = sb_runtime_call(runtime, bytes(words[1]), strlen(words[1]), SB_ASSISTANT, &response);
    }
    else if (count == 2 && strcmp(command, "outcome") == 0)
    {
        sb_outcome outcome;
        memset(&outcome, 0, sizeof(outcome));
        status = sb_runtime_call_outcome(runtime, bytes(words[1]), strlen(words[1]), SB_ASSISTANT, &outcome, &response);
        return print_outcome(status, &outcome, &response);
    }
    else if (count == 2 && strcmp(command, "rpc") == 0)
    {
        status = sb_runtime_rpc(runtime, bytes(words[1]), strlen(words[1]), &response);
    }
    else if (count == 2 && strcmp(command, "approval") == 0)
    {
        status = sb_runtime_approval(runtime, bytes(words[1]), strlen(words[1]), &response);
    }
    else if (count == 2 && strcmp(command, "approve") == 0)
    {
        status = sb_runtime_approve(runtime, bytes(words[1]), strlen(words[1]), &response);
    }
    else if (count == 2 && strcmp(command, "deny") == 0)
    {
        status = sb_runtime_deny(runtime, bytes(words[1]), strlen(words[1]), &response);
    }
    else if (count <= 2 && strcmp(command, "actions") == 0)
    {
        const char *filter = count == 2 ? words[1] : "";
        status = sb_runtime_actions(runtime, bytes(filter), strlen(filter), &response);
    }
    else if (count == 4 && strcmp(command, "reconcile") == 0)
    {
        size_t length = 0;
        char *request = reconcile_request(words[1], words[2], words[3], &length);
        if (request == NULL)
        {
            fputs("ERROR: the native C reconcile request is too large or could not be allocated\n", stderr);
            return 1;
        }
        status = sb_runtime_reconcile(runtime, bytes(request), length, &response);
        free(request);
    }
    else if (count == 2 && strcmp(command, "kill-switch") == 0)
    {
        int32_t mode = -1;
        if (!kill_switch_mode(words[1], &mode))
        {
            fputs("ERROR: kill-switch takes status, on or off\n", stderr);
            return 1;
        }
        status = sb_runtime_kill_switch(runtime, mode, &response);
    }
    else if (count == 2 && strcmp(command, "backup") == 0)
    {
        status = sb_runtime_backup(runtime, bytes(words[1]), strlen(words[1]), &response);
    }
    else if (count == 2 && strcmp(command, "purge") == 0 && (strcmp(words[1], "dry-run") == 0 || strcmp(words[1], "apply") == 0))
    {
        status = sb_runtime_purge(runtime, strcmp(words[1], "dry-run") == 0 ? 1 : 0, &response);
    }
    else
    {
        fputs("ERROR: unsupported native C command or argument count\n", stderr);
        return 1;
    }
    return print_result(status, &response);
}

static uint8_t *read_configuration(const char *path, size_t *size)
{
    FILE *file = fopen(path, "rb");
    uint8_t *configuration = NULL;
    int read_failed = 0;
    int close_failed = 0;
    if (file == NULL)
    {
        fputs("ERROR: cannot open the native C configuration\n", stderr);
        return NULL;
    }
    configuration = malloc((size_t)SB_MAX_INPUT_BYTES + 1);
    if (configuration == NULL)
    {
        fclose(file);
        fputs("ERROR: cannot allocate native C configuration input\n", stderr);
        return NULL;
    }
    *size = fread(configuration, 1, (size_t)SB_MAX_INPUT_BYTES + 1, file);
    read_failed = ferror(file);
    close_failed = fclose(file);
    if (read_failed || close_failed != 0 || *size > SB_MAX_INPUT_BYTES)
    {
        free(configuration);
        fputs("ERROR: native C configuration is unreadable or too large\n", stderr);
        return NULL;
    }
    return configuration;
}

int main(const int argc, char **argv)
{
    sb_runtime *runtime = NULL;
    sb_buffer response = {NULL, 0};
    uint8_t *configuration = NULL;
    size_t size = 0;
    sb_status created = SB_INVALID_ARGUMENT;
    int exit_code = 1;
    if (argc < 3 || argc > 6)
    {
        fputs(USAGE, stderr);
        return 2;
    }
    if (sb_abi_version() != SB_ABI_VERSION || sb_abi_revision() < SB_ABI_REVISION)
    {
        fputs("ERROR: native C library version does not match this client\n", stderr);
        return 1;
    }
    if (argc == 3 && strcmp(argv[1], "verify-backup") == 0)
    {
        /* Verification opens the backup directory read-only and needs no configuration or runtime. */
        const sb_status verified = sb_verify_backup(bytes(argv[2]), strlen(argv[2]), &response);
        return print_result(verified, &response);
    }
    configuration = read_configuration(argv[1], &size);
    if (configuration == NULL)
    {
        return 1;
    }
    created = sb_runtime_create(configuration, size, &runtime, &response);
    free(configuration);
    if (created != SB_OK)
    {
        return print_result(created, &response);
    }
    exit_code = run_command(runtime, argc - 2, argv + 2);
    sb_runtime_destroy(runtime);
    return exit_code;
}
