#include "switchboard/native.hpp"
#include "switchboard/switchboard.h"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <filesystem>
#include <memory>
#include <new>
#include <optional>
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

constexpr std::size_t IDENTIFIER_LENGTH = 32;
static_assert(sizeof(sb_outcome{}.approval_id) == IDENTIFIER_LENGTH + 1 && sizeof(sb_outcome{}.action_id) == IDENTIFIER_LENGTH + 1);

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

struct CallRequest
{
    std::string name;
    Json arguments;
    switchboard::Origin origin;
};

CallRequest call_request(const uint8_t *request, const size_t length, const sb_origin origin)
{
    const auto message = switchboard::parse_json(input(request, length));
    switchboard::check_object(message, {"tool", "arguments"}, {"tool"});
    CallRequest result{switchboard::string_field(message, "tool", 64, true, 1), message.value("arguments", Json::object()), switchboard::Origin::assistant};
    switch (origin)
    {
    case SB_ASSISTANT:
        result.origin = switchboard::Origin::assistant;
        break;
    case SB_RULE:
        result.origin = switchboard::Origin::rule;
        break;
    case SB_HUMAN:
        result.origin = switchboard::Origin::human;
        break;
    default:
        throw Error("invalid", "native C origin is invalid");
    }
    return result;
}

// Paths become bounded UTF-8 text without NUL bytes, so engine reports can quote them as JSON text.
std::filesystem::path path_input(const uint8_t *data, const size_t length)
{
    return std::filesystem::path(switchboard::string_field(Json{{"path", input(data, length)}}, "path", 4096, true, 1));
}

const Json *member(const Json &object, const char *key) noexcept
{
    if (!object.is_object())
    {
        return nullptr;
    }
    const auto found = object.find(key);
    return found == object.end() ? nullptr : &*found;
}

// Writes a 32-character lowercase hexadecimal identifier, or leaves the destination empty.
void copy_identifier(const Json *value, char (&destination)[IDENTIFIER_LENGTH + 1]) noexcept
{
    destination[0] = '\0';
    if (value == nullptr || !value->is_string())
    {
        return;
    }
    const auto &text = value->get_ref<const std::string &>();
    const auto hexadecimal = [](const char character)
    {
        return (character >= '0' && character <= '9') || (character >= 'a' && character <= 'f');
    };
    if (text.size() != IDENTIFIER_LENGTH || !std::all_of(text.begin(), text.end(), hexadecimal))
    {
        return;
    }
    std::memcpy(destination, text.data(), IDENTIFIER_LENGTH);
    destination[IDENTIFIER_LENGTH] = '\0';
}

int32_t outcome_status(const Json &report) noexcept
{
    const auto *status = member(report, "status");
    if (status == nullptr || !status->is_string())
    {
        return SB_OUTCOME_ERROR;
    }
    const auto &text = status->get_ref<const std::string &>();
    if (text == "ok")
    {
        return SB_OUTCOME_OK;
    }
    if (text == "pending")
    {
        return SB_OUTCOME_PENDING;
    }
    if (text == "denied")
    {
        return SB_OUTCOME_DENIED;
    }
    if (text == "invalid")
    {
        return SB_OUTCOME_INVALID;
    }
    return SB_OUTCOME_ERROR;
}

// Applies the typed-outcome table in contracts/README.md. The journal entry decides first: any report without
// one (a read, pending approval or refusal before execution) has no effect, whatever its status. A `failed`
// entry stays failed under `invalid` or `error`, as when a write handler rejects its arguments after the attempt
// was recorded. The status only guards against contradictions, and contradictory or malformed journal data never
// upgrades an effect: it maps to SB_EFFECT_UNKNOWN.
int32_t outcome_effect(const Json &report, const int32_t status) noexcept
{
    const auto *action = member(report, "action");
    if (action == nullptr || action->is_null())
    {
        return SB_EFFECT_NONE;
    }
    const auto *state = member(*action, "state");
    if (state == nullptr || !state->is_string())
    {
        return SB_EFFECT_UNKNOWN;
    }
    const auto &text = state->get_ref<const std::string &>();
    if (text == "failed")
    {
        return status == SB_OUTCOME_OK ? SB_EFFECT_UNKNOWN : SB_EFFECT_FAILED;
    }
    if (text != "succeeded" || status != SB_OUTCOME_OK)
    {
        return SB_EFFECT_UNKNOWN;
    }
    const auto *data = member(report, "data");
    const auto *unknown = data == nullptr ? nullptr : member(*data, "effect_unknown");
    if (unknown != nullptr && unknown->is_boolean() && unknown->get<bool>())
    {
        return SB_EFFECT_UNKNOWN;
    }
    const auto *confirmed = data == nullptr ? nullptr : member(*data, "confirmed");
    if (confirmed != nullptr && confirmed->is_boolean())
    {
        return confirmed->get<bool>() ? SB_EFFECT_CONFIRMED : SB_EFFECT_UNCONFIRMED;
    }
    return SB_EFFECT_RETURNED;
}

void describe(const Json &report, sb_outcome &outcome) noexcept
{
    outcome.status = outcome_status(report);
    outcome.effect = outcome_effect(report, outcome.status);
    copy_identifier(member(report, "approval_id"), outcome.approval_id);
    const auto *action = member(report, "action");
    copy_identifier(action == nullptr ? nullptr : member(*action, "id"), outcome.action_id);
}
}  // namespace

extern "C"
{
    uint32_t sb_abi_version(void)
    {
        return SB_ABI_VERSION;
    }

    uint32_t sb_abi_revision(void)
    {
        return SB_ABI_REVISION;
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
                           const auto call = call_request(request, length, origin);
                           return runtime_value(runtime).call(call.name, call.arguments, call.origin);
                       });
    }

    sb_status sb_runtime_call_outcome(sb_runtime *runtime, const uint8_t *request, const size_t length, const sb_origin origin, sb_outcome *outcome,
                                      sb_buffer *report)
    {
        if (outcome != nullptr)
        {
            *outcome = sb_outcome{};
        }
        enum class Stage
        {
            checking,
            running,
            described
        };
        auto stage = Stage::checking;
        const auto status = guarded(report,
                                    [&]
                                    {
                                        if (outcome == nullptr)
                                        {
                                            throw Error("invalid", "native C typed outcome requires an outcome destination");
                                        }
                                        const auto call = call_request(request, length, origin);
                                        auto &engine = runtime_value(runtime);
                                        stage = Stage::running;
                                        auto result = engine.call(call.name, call.arguments, call.origin);
                                        describe(result, *outcome);
                                        stage = Stage::described;
                                        return result;
                                    });
        // A described outcome stays valid even if its report could not be returned. Without one, an engine
        // failure may have interrupted an attempt, so its effect is unknown rather than none.
        if (status != SB_OK && outcome != nullptr && stage != Stage::described)
        {
            outcome->status = SB_OUTCOME_ERROR;
            outcome->effect = stage == Stage::running ? SB_EFFECT_UNKNOWN : SB_EFFECT_NONE;
        }
        return status;
    }

    sb_status sb_runtime_approval(sb_runtime *runtime, const uint8_t *id, const size_t length, sb_buffer *output)
    {
        return guarded(output,
                       [&]() -> Json
                       {
                           auto &engine = runtime_value(runtime);
                           const auto text = input(id, length);
                           try
                           {
                               return engine.approval(text);
                           }
                           catch (const Error &error)
                           {
                               // As with approve and deny, a malformed or unknown identifier is an invalid report,
                               // not an interface failure; other errors still fail the call.
                               if (error.code() != "invalid")
                               {
                                   throw;
                               }
                               return Json{{"status", "invalid"}, {"tool", "approvals"}, {"message", error.what()}};
                           }
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

    sb_status sb_runtime_actions(sb_runtime *runtime, const uint8_t *filter, const size_t length, sb_buffer *output)
    {
        return guarded(output,
                       [&]
                       {
                           const auto text = input(filter, length);
                           const auto selection = text.empty() ? Json::object() : switchboard::parse_json(text);
                           if (!selection.is_object())
                           {
                               throw Error("invalid", "native C action filter must be a JSON object");
                           }
                           return runtime_value(runtime).actions(selection);
                       });
    }

    sb_status sb_runtime_reconcile(sb_runtime *runtime, const uint8_t *request, const size_t length, sb_buffer *output)
    {
        return guarded(output,
                       [&]
                       {
                           const auto message = switchboard::parse_json(input(request, length));
                           switchboard::check_object(message, {"id", "resolution", "note"}, {"id", "resolution", "note"});
                           const auto id = switchboard::string_field(message, "id", 64, true, 1);
                           const auto resolution = switchboard::string_field(message, "resolution", 32, true, 1);
                           const auto note = switchboard::string_field(message, "note", 4096);
                           return runtime_value(runtime).reconcile(id, resolution, note);
                       });
    }

    sb_status sb_runtime_kill_switch(sb_runtime *runtime, const int32_t mode, sb_buffer *output)
    {
        return guarded(output,
                       [&]
                       {
                           std::optional<bool> enable;
                           switch (mode)
                           {
                           case -1:
                               break;
                           case 0:
                               enable = false;
                               break;
                           case 1:
                               enable = true;
                               break;
                           default:
                               throw Error("invalid", "native C kill switch mode must be -1, 0 or 1");
                           }
                           return runtime_value(runtime).kill_switch(enable);
                       });
    }

    sb_status sb_runtime_backup(sb_runtime *runtime, const uint8_t *path, const size_t length, sb_buffer *output)
    {
        return guarded(output,
                       [&]
                       {
                           const auto destination = path_input(path, length);
                           return runtime_value(runtime).backup(destination);
                       });
    }

    sb_status sb_runtime_purge(sb_runtime *runtime, const int32_t dry_run, sb_buffer *output)
    {
        return guarded(output,
                       [&]
                       {
                           if (dry_run != 0 && dry_run != 1)
                           {
                               throw Error("invalid", "native C purge dry_run must be 0 or 1");
                           }
                           return runtime_value(runtime).purge(dry_run == 1);
                       });
    }

    sb_status sb_verify_backup(const uint8_t *path, const size_t length, sb_buffer *output)
    {
        return guarded(output,
                       [&]
                       {
                           return switchboard::Runtime::verify_backup(path_input(path, length));
                       });
    }

    void sb_runtime_cancel(sb_runtime *runtime)
    {
        // No lock, allocation or exception: Runtime::cancel is noexcept and only signals the call in progress.
        if (runtime != nullptr && runtime->value)
        {
            runtime->value->cancel();
        }
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
