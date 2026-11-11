#include "support.hpp"
#include "switchboard/switchboard.h"

#include <cstdint>
#include <string>

using switchboard::test::expect;

namespace
{
std::string take(sb_buffer &buffer)
{
    const std::string result = buffer.length == 0 ? "" : std::string(reinterpret_cast<const char *>(buffer.data), buffer.length);
    sb_buffer_free(&buffer);
    expect(buffer.data == nullptr && buffer.length == 0, "native C buffer did not reset");
    return result;
}
}  // namespace

int main()
{
    static_assert(sizeof(sb_status) == sizeof(std::int32_t));
    static_assert(sizeof(sb_origin) == sizeof(std::int32_t));
    expect(sb_abi_version() == SB_ABI_VERSION, "native C interface version differs");
    sb_runtime *runtime = nullptr;
    sb_buffer output{nullptr, 0};
    const std::string malformed = "{\"unexpected\":true}";
    expect(sb_runtime_create(reinterpret_cast<const uint8_t *>(malformed.data()), malformed.size(), &runtime, &output) == SB_INVALID_ARGUMENT,
           "native C accepted invalid configuration");
    expect(runtime == nullptr, "native C published a failed runtime");
    expect(!take(output).empty(), "native C invalid configuration had no error report");

    switchboard::test::Directory directory;
    const auto configuration = switchboard::test::configuration(directory, {}).dump();
    expect(sb_runtime_create(reinterpret_cast<const uint8_t *>(configuration.data()), configuration.size(), &runtime, &output) == SB_OK,
           "native C runtime creation failed");
    expect(runtime != nullptr && output.data == nullptr, "native C creation ownership differs");
    expect(sb_runtime_catalog(runtime, &output) == SB_OK, "native C catalog failed");
    expect(sb_runtime_catalog(runtime, &output) == SB_INVALID_ARGUMENT, "native C overwrote an owned result");
    expect(take(output).find("switchboard_status") != std::string::npos, "native C catalog lacks core tools");

    const std::string request = "{\"tool\":\"switchboard_status\",\"arguments\":{}}";
    expect(sb_runtime_call(runtime, reinterpret_cast<const uint8_t *>(request.data()), request.size(), SB_ASSISTANT, &output) == SB_OK,
           "native C core call failed");
    expect(take(output).find("\"python_required\":false") != std::string::npos, "native C call is not a standalone native engine");
    expect(sb_runtime_call(runtime, nullptr, 1, SB_ASSISTANT, &output) == SB_INVALID_ARGUMENT, "native C accepted an invalid pointer/count");
    take(output);
    expect(sb_runtime_call(runtime, reinterpret_cast<const uint8_t *>(request.data()), request.size(), 99, &output) == SB_INVALID_ARGUMENT,
           "native C accepted an invalid origin");
    take(output);
    sb_buffer_free(&output);
    sb_runtime_destroy(runtime);
    return 0;
}
