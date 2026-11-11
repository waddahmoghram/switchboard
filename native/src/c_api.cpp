#include "switchboard/native.hpp"
#include "switchboard/switchboard.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <memory>
#include <new>
#include <string>
#include <utility>

struct sb_runtime
{
    std::unique_ptr<switchboard::Runtime> value;
};

namespace
{
using switchboard::Error;
using switchboard::Json;

bool empty_output(const sb_buffer *output)
{
    return output != nullptr && output->data == nullptr && output->length == 0;
}

std::string input(const uint8_t *data, const size_t length)
{
    if (length > SB_MAX_INPUT_BYTES || (data == nullptr && length != 0))
    {
        throw Error("invalid", "native C input pointer or byte count is invalid");
    }
    return length == 0 ? std::string() : std::string(reinterpret_cast<const char *>(data), length);
}

void write(sb_buffer *output, const Json &value)
{
    const auto text = value.dump();
    if (text.size() > 8 * switchboard::MAX_REQUEST_BYTES)
    {
        throw Error("limit", "native C result exceeds its output limit");
    }
    auto *memory = static_cast<uint8_t *>(std::malloc(text.size() + 1));
    if (memory == nullptr)
    {
        throw std::bad_alloc();
    }
    std::memcpy(memory, text.data(), text.size());
    memory[text.size()] = 0;
    output->data = memory;
    output->length = text.size();
}

sb_status failure(sb_buffer *output, const sb_status status, const char *code, const char *message) noexcept
{
    try
    {
        write(output, Json{{"status", "error"}, {"code", code}, {"message", message}});
    }
    catch (...)
    {
        // The C boundary must never propagate an exception, including during error allocation.
        std::fputs("ERROR: native C error report could not be allocated\n", stderr);
        return SB_ALLOCATION_ERROR;
    }
    return status;
}

template <typename Function> sb_status guarded(sb_buffer *output, Function function, const bool write_result = true) noexcept
{
    if (!empty_output(output))
    {
        return SB_INVALID_ARGUMENT;
    }
    try
    {
        const auto result = function();
        if (write_result)
        {
            write(output, result);
        }
        return SB_OK;
    }
    catch (const Error &error)
    {
        return failure(output, error.code() == "invalid" || error.code() == "schema" ? SB_INVALID_ARGUMENT : SB_RUNTIME_ERROR, error.code().c_str(),
                       error.what());
    }
    catch (const std::bad_alloc &)
    {
        std::fputs("ERROR: native C allocation failed\n", stderr);
        return SB_ALLOCATION_ERROR;
    }
    catch (const std::exception &)
    {
        return failure(output, SB_RUNTIME_ERROR, "internal", "unexpected native C failure; inspect state before retrying external effects");
    }
    catch (...)
    {
        return failure(output, SB_RUNTIME_ERROR, "internal", "unknown native C failure; no exception crossed the C boundary");
    }
}

switchboard::Runtime &runtime_value(sb_runtime *runtime)
{
    if (runtime == nullptr || !runtime->value)
    {
        throw Error("invalid", "native C runtime handle is missing");
    }
    return *runtime->value;
}
}  // namespace

extern "C"
{
    uint32_t sb_abi_version(void)
    {
        return SB_ABI_VERSION;
    }

    sb_status sb_runtime_create(const uint8_t *configuration, const size_t length, sb_runtime **output, sb_buffer *error)
    {
        return guarded(
            error,
            [&]() -> Json
            {
                if (output == nullptr || *output != nullptr)
                {
                    throw Error("invalid", "native C creation requires an initially null output handle");
                }
                auto handle = std::make_unique<sb_runtime>();
                handle->value = std::make_unique<switchboard::Runtime>(switchboard::parse_json(input(configuration, length)));
                *output = handle.release();
                return nullptr;
            },
            false);
    }

    void sb_runtime_destroy(sb_runtime *runtime)
    {
        delete runtime;
    }

    sb_status sb_runtime_catalog(sb_runtime *runtime, sb_buffer *output)
    {
        return guarded(output,
                       [&]
                       {
                           return runtime_value(runtime).catalog();
                       });
    }

    sb_status sb_runtime_call(sb_runtime *runtime, const uint8_t *request, const size_t length, const sb_origin origin, sb_buffer *output)
    {
        return guarded(output,
                       [&]
                       {
                           const auto message = switchboard::parse_json(input(request, length));
                           switchboard::check_object(message, {"tool", "arguments"}, {"tool"});
                           const auto name = switchboard::string_field(message, "tool", 64, true, 1);
                           switchboard::Origin selected;
                           switch (origin)
                           {
                           case SB_ASSISTANT:
                               selected = switchboard::Origin::assistant;
                               break;
                           case SB_RULE:
                               selected = switchboard::Origin::rule;
                               break;
                           case SB_HUMAN:
                               selected = switchboard::Origin::human;
                               break;
                           default:
                               throw Error("invalid", "native C origin is invalid");
                           }
                           return runtime_value(runtime).call(name, message.value("arguments", Json::object()), selected);
                       });
    }

    sb_status sb_runtime_approve(sb_runtime *runtime, const uint8_t *id, const size_t length, sb_buffer *output)
    {
        return guarded(output,
                       [&]
                       {
                           return runtime_value(runtime).approve(input(id, length));
                       });
    }

    sb_status sb_runtime_deny(sb_runtime *runtime, const uint8_t *id, const size_t length, sb_buffer *output)
    {
        return guarded(output,
                       [&]
                       {
                           return runtime_value(runtime).deny(input(id, length));
                       });
    }

    sb_status sb_runtime_tick(sb_runtime *runtime, sb_buffer *output)
    {
        return guarded(output,
                       [&]
                       {
                           return runtime_value(runtime).tick();
                       });
    }

    sb_status sb_runtime_rpc(sb_runtime *runtime, const uint8_t *request, const size_t length, sb_buffer *output)
    {
        return guarded(output,
                       [&]
                       {
                           return runtime_value(runtime).rpc(switchboard::parse_json(input(request, length)));
                       });
    }

    void sb_buffer_free(sb_buffer *buffer)
    {
        if (buffer != nullptr)
        {
            std::free(buffer->data);
            buffer->data = nullptr;
            buffer->length = 0;
        }
    }
}
