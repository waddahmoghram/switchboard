#include "switchboard/switchboard.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

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

int main(const int argc, char **argv)
{
    if (argc < 3 || argc > 4)
    {
        fputs("Usage: switchboard-c CONFIG.json catalog|call|rpc|approve|deny|tick [JSON_OR_ID]\n", stderr);
        return 2;
    }
    if (sb_abi_version() != SB_ABI_VERSION)
    {
        fputs("ERROR: native C library version does not match this client\n", stderr);
        return 1;
    }
    FILE *file = fopen(argv[1], "rb");
    if (file == NULL)
    {
        fputs("ERROR: cannot open the native C configuration\n", stderr);
        return 1;
    }
    uint8_t *configuration = malloc((size_t)SB_MAX_INPUT_BYTES + 1);
    if (configuration == NULL)
    {
        fclose(file);
        fputs("ERROR: cannot allocate native C configuration input\n", stderr);
        return 1;
    }
    const size_t size = fread(configuration, 1, (size_t)SB_MAX_INPUT_BYTES + 1, file);
    const int read_failed = ferror(file);
    const int close_failed = fclose(file);
    if (read_failed || close_failed != 0 || size > SB_MAX_INPUT_BYTES)
    {
        free(configuration);
        fputs("ERROR: native C configuration is unreadable or too large\n", stderr);
        return 1;
    }
    sb_runtime *runtime = NULL;
    sb_buffer response = {NULL, 0};
    const sb_status created = sb_runtime_create(configuration, size, &runtime, &response);
    free(configuration);
    if (created != SB_OK)
    {
        return print_result(created, &response);
    }
    sb_status result = SB_INVALID_ARGUMENT;
    const char *argument = argc == 4 ? argv[3] : "";
    const size_t length = strlen(argument);
    if (strcmp(argv[2], "catalog") == 0 && argc == 3)
    {
        result = sb_runtime_catalog(runtime, &response);
    }
    else if (strcmp(argv[2], "tick") == 0 && argc == 3)
    {
        result = sb_runtime_tick(runtime, &response);
    }
    else if (strcmp(argv[2], "call") == 0 && argc == 4)
    {
        result = sb_runtime_call(runtime, (const uint8_t *)argument, length, SB_ASSISTANT, &response);
    }
    else if (strcmp(argv[2], "rpc") == 0 && argc == 4)
    {
        result = sb_runtime_rpc(runtime, (const uint8_t *)argument, length, &response);
    }
    else if (strcmp(argv[2], "approve") == 0 && argc == 4)
    {
        result = sb_runtime_approve(runtime, (const uint8_t *)argument, length, &response);
    }
    else if (strcmp(argv[2], "deny") == 0 && argc == 4)
    {
        result = sb_runtime_deny(runtime, (const uint8_t *)argument, length, &response);
    }
    else
    {
        fputs("ERROR: unsupported native C command or argument count\n", stderr);
    }
    const int exit_code = print_result(result, &response);
    sb_runtime_destroy(runtime);
    return exit_code;
}
