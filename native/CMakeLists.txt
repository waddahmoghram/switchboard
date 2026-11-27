cmake_minimum_required(VERSION 3.20)
project(SwitchboardNative VERSION 0.1.0 LANGUAGES C CXX)

include(GNUInstallDirs)
include(CMakePackageConfigHelpers)

option(SWITCHBOARD_BUILD_TESTS "Build the native tests; running them is a separate step" OFF)
option(SWITCHBOARD_BUILD_FUZZERS "Build libFuzzer targets; requires LLVM Clang and SWITCHBOARD_SANITIZE" OFF)
option(SWITCHBOARD_WARNINGS_AS_ERRORS "Treat compiler warnings in Switchboard targets as errors" OFF)
option(SWITCHBOARD_COVERAGE "Instrument Switchboard targets for gcov-format line coverage" OFF)
set(SWITCHBOARD_SANITIZE "" CACHE STRING "Sanitizers as a CMake list: address, undefined, leak, thread")
# The shared library's SONAME tracks SB_ABI_VERSION in include/switchboard/switchboard.h; bump them together.
set(SWITCHBOARD_SOVERSION 1)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)

set(SWITCHBOARD_KNOWN_SANITIZERS address undefined leak thread)
foreach(sanitizer IN LISTS SWITCHBOARD_SANITIZE)
    if(NOT sanitizer IN_LIST SWITCHBOARD_KNOWN_SANITIZERS)
        message(FATAL_ERROR "SWITCHBOARD_SANITIZE: unsupported sanitizer '${sanitizer}'")
    endif()
endforeach()
if("thread" IN_LIST SWITCHBOARD_SANITIZE AND ("address" IN_LIST SWITCHBOARD_SANITIZE OR "leak" IN_LIST SWITCHBOARD_SANITIZE))
    message(FATAL_ERROR "SWITCHBOARD_SANITIZE: thread cannot be combined with address or leak")
endif()
if(SWITCHBOARD_SANITIZE OR SWITCHBOARD_COVERAGE)
    if(NOT CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
        message(FATAL_ERROR "SWITCHBOARD_SANITIZE and SWITCHBOARD_COVERAGE require GCC or Clang")
    endif()
endif()
if(SWITCHBOARD_BUILD_FUZZERS)
    if(NOT CMAKE_CXX_COMPILER_ID STREQUAL "Clang")
        message(FATAL_ERROR "SWITCHBOARD_BUILD_FUZZERS requires LLVM Clang; Apple Clang does not ship libFuzzer")
    endif()
    # The instrumented shared library calls coverage hooks; a sanitizer runtime supplies them to every
    # non-fuzzer executable that links it. libFuzzer does not support ThreadSanitizer.
    if(NOT SWITCHBOARD_SANITIZE OR "thread" IN_LIST SWITCHBOARD_SANITIZE)
        message(FATAL_ERROR "SWITCHBOARD_BUILD_FUZZERS requires SWITCHBOARD_SANITIZE with address and/or undefined")
    endif()
endif()

if(SWITCHBOARD_SANITIZE)
    list(JOIN SWITCHBOARD_SANITIZE "," SWITCHBOARD_SANITIZE_FLAG)
    add_compile_options(-fsanitize=${SWITCHBOARD_SANITIZE_FLAG} -fno-omit-frame-pointer)
    add_link_options(-fsanitize=${SWITCHBOARD_SANITIZE_FLAG})
    if("undefined" IN_LIST SWITCHBOARD_SANITIZE)
        add_compile_options(-fno-sanitize-recover=undefined)
    endif()
endif()
if(SWITCHBOARD_COVERAGE)
    add_compile_options(--coverage)
    add_link_options(--coverage)
endif()

# Adds the project's warning flags to one target, as errors when SWITCHBOARD_WARNINGS_AS_ERRORS is on.
function(switchboard_warnings target)
    if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
        target_compile_options(${target} PRIVATE -Wall -Wextra -Wpedantic)
        if(SWITCHBOARD_WARNINGS_AS_ERRORS)
            target_compile_options(${target} PRIVATE -Werror)
        endif()
    endif()
endfunction()

find_package(CURL REQUIRED)
find_package(SQLite3 REQUIRED)
find_package(nlohmann_json 3.10 REQUIRED)
find_package(OpenSSL REQUIRED COMPONENTS Crypto)
find_package(Threads REQUIRED)

add_library(switchboard_native SHARED
    src/foundation.cpp
    src/audit.cpp
    src/core.cpp
    src/transport.cpp
    src/digest.cpp
    src/home.cpp
    src/devices.cpp
    src/personal.cpp
    src/monitoring.cpp
    src/broker.cpp
    src/c_api.cpp
)
add_library(switchboard::native ALIAS switchboard_native)
# Symbols keep the default visibility on purpose: the installed C++ interface (native.hpp) has no export
# macros, and native/tests/broker.cpp, audit.cpp and contracts.cpp link internal functions declared in
# src/broker_internal.hpp and src/audit_internal.hpp from this shared library. Hiding symbols would require
# building such tests from an object library instead.
target_include_directories(switchboard_native PUBLIC
    "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>"
    "$<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>"
)
target_link_libraries(switchboard_native
    PRIVATE CURL::libcurl SQLite::SQLite3 OpenSSL::Crypto Threads::Threads
    PUBLIC nlohmann_json::nlohmann_json
)
target_compile_definitions(switchboard_native PRIVATE SWITCHBOARD_NATIVE_BUILD)
set_target_properties(switchboard_native PROPERTIES
    VERSION "${PROJECT_VERSION}"
    SOVERSION "${SWITCHBOARD_SOVERSION}"
    EXPORT_NAME native
)
switchboard_warnings(switchboard_native)

add_executable(switchboard-native src/main.cpp)
target_link_libraries(switchboard-native PRIVATE switchboard_native)
switchboard_warnings(switchboard-native)
add_executable(switchboard-c clients/c/main.c)
target_link_libraries(switchboard-c PRIVATE switchboard_native)
set_target_properties(switchboard-c PROPERTIES LINKER_LANGUAGE CXX)
switchboard_warnings(switchboard-c)

# Installed programs find the library relative to their own location, so a prefix can be moved.
if(APPLE)
    set(SWITCHBOARD_RPATH_ORIGIN "@loader_path")
else()
    set(SWITCHBOARD_RPATH_ORIGIN "$ORIGIN")
endif()
file(RELATIVE_PATH SWITCHBOARD_BINDIR_TO_LIBDIR "${CMAKE_INSTALL_FULL_BINDIR}" "${CMAKE_INSTALL_FULL_LIBDIR}")
set_target_properties(switchboard-native switchboard-c PROPERTIES
    INSTALL_RPATH "${SWITCHBOARD_RPATH_ORIGIN}/${SWITCHBOARD_BINDIR_TO_LIBDIR}"
)

if(SWITCHBOARD_BUILD_TESTS)
    enable_testing()
    set(SWITCHBOARD_TEST_SUITES
        core
        audit
        home
        devices
        personal
        monitoring
        c_api
        recovery
        lifecycle
        broker
        contracts
        faults
    )
    # Replacing global operator new conflicts with the static ThreadSanitizer and LeakSanitizer runtimes.
    if(NOT "thread" IN_LIST SWITCHBOARD_SANITIZE AND NOT "leak" IN_LIST SWITCHBOARD_SANITIZE)
        list(APPEND SWITCHBOARD_TEST_SUITES alloc_faults)
    endif()
    foreach(suite IN LISTS SWITCHBOARD_TEST_SUITES)
        add_executable(native-test-${suite} tests/${suite}.cpp)
        # Threads::Threads for suites that start threads (c_api cancellation, broker, recovery, lifecycle).
        target_link_libraries(native-test-${suite} PRIVATE switchboard_native Threads::Threads)
        switchboard_warnings(native-test-${suite})
        add_test(NAME native-${suite} COMMAND native-test-${suite})
        set_tests_properties(native-${suite} PROPERTIES LABELS "native" TIMEOUT 300)
    endforeach()
    if(TARGET native-test-alloc_faults)
        # dladdr, used to recognize allocations inside the JSON library's destructor, needs libdl before glibc 2.34.
        target_link_libraries(native-test-alloc_faults PRIVATE ${CMAKE_DL_LIBS})
    endif()
    add_executable(native-test-c-client tests/c_client.c)
    target_link_libraries(native-test-c-client PRIVATE switchboard_native)
    set_target_properties(native-test-c-client PROPERTIES LINKER_LANGUAGE CXX)
    switchboard_warnings(native-test-c-client)
    add_test(NAME native-c-client COMMAND native-test-c-client)
    set_tests_properties(native-c-client PROPERTIES LABELS "native" TIMEOUT 300)

    get_filename_component(SWITCHBOARD_CONTRACTS_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../contracts/cases" ABSOLUTE)
    target_compile_definitions(native-test-contracts PRIVATE "SWITCHBOARD_CONTRACTS_DIR=\"${SWITCHBOARD_CONTRACTS_DIR}\"")
    set_tests_properties(native-contracts PROPERTIES LABELS "native;contracts")
    set_tests_properties(native-faults PROPERTIES LABELS "native;faults" TIMEOUT 900)
    # Thread-sensitive suites (broker sockets, cancellation, recovery, lifecycle) form the ThreadSanitizer selection.
    set_tests_properties(native-broker native-recovery native-lifecycle PROPERTIES LABELS "native;concurrency")
    if(TEST native-alloc_faults)
        # Exit code 77 reports that operator new replacement does not reach the library on this platform.
        set_tests_properties(native-alloc_faults PROPERTIES LABELS "native;faults" TIMEOUT 3600 SKIP_RETURN_CODE 77)
    endif()
endif()

if(SWITCHBOARD_BUILD_FUZZERS)
    enable_testing()
    target_compile_options(switchboard_native PRIVATE -fsanitize=fuzzer-no-link)
    foreach(fuzzer IN ITEMS json schema timestamp endpoint digest c_api)
        add_executable(native-fuzz-${fuzzer} fuzz/${fuzzer}.cpp)
        target_link_libraries(native-fuzz-${fuzzer} PRIVATE switchboard_native)
        target_compile_options(native-fuzz-${fuzzer} PRIVATE -fsanitize=fuzzer)
        target_link_options(native-fuzz-${fuzzer} PRIVATE -fsanitize=fuzzer)
        switchboard_warnings(native-fuzz-${fuzzer})
        # Replays the committed seeds once without mutation; time-bounded fuzzing is a separate command.
        add_test(NAME native-fuzz-${fuzzer}-corpus
            COMMAND native-fuzz-${fuzzer} -runs=0 "${CMAKE_CURRENT_SOURCE_DIR}/fuzz/corpus/${fuzzer}"
        )
        set_tests_properties(native-fuzz-${fuzzer}-corpus PROPERTIES LABELS "fuzz-corpus" TIMEOUT 300)
    endforeach()
endif()

install(TARGETS switchboard_native
    EXPORT SwitchboardNativeTargets
    LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}" COMPONENT Runtime NAMELINK_COMPONENT Development
    ARCHIVE DESTINATION "${CMAKE_INSTALL_LIBDIR}" COMPONENT Development
    RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}" COMPONENT Runtime
)
install(TARGETS switchboard-native switchboard-c RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}" COMPONENT Runtime)
install(DIRECTORY include/switchboard DESTINATION "${CMAKE_INSTALL_INCLUDEDIR}" COMPONENT Development)

set(SWITCHBOARD_CMAKE_DIR "${CMAKE_INSTALL_LIBDIR}/cmake/SwitchboardNative")
install(EXPORT SwitchboardNativeTargets
    NAMESPACE switchboard::
    DESTINATION "${SWITCHBOARD_CMAKE_DIR}"
    COMPONENT Development
)
configure_package_config_file(cmake/SwitchboardNativeConfig.cmake.in
    "${CMAKE_CURRENT_BINARY_DIR}/SwitchboardNativeConfig.cmake"
    INSTALL_DESTINATION "${SWITCHBOARD_CMAKE_DIR}"
)
# Before 1.0, a new minor version may change the interface; patch releases within a minor version are compatible.
write_basic_package_version_file("${CMAKE_CURRENT_BINARY_DIR}/SwitchboardNativeConfigVersion.cmake"
    VERSION "${PROJECT_VERSION}"
    COMPATIBILITY SameMinorVersion
)
install(FILES
    "${CMAKE_CURRENT_BINARY_DIR}/SwitchboardNativeConfig.cmake"
    "${CMAKE_CURRENT_BINARY_DIR}/SwitchboardNativeConfigVersion.cmake"
    DESTINATION "${SWITCHBOARD_CMAKE_DIR}"
    COMPONENT Development
)

# pkg-config paths stay relative to the installed .pc file when the install directories are relative.
if(IS_ABSOLUTE "${CMAKE_INSTALL_LIBDIR}" OR IS_ABSOLUTE "${CMAKE_INSTALL_INCLUDEDIR}")
    set(SWITCHBOARD_PC_PREFIX "${CMAKE_INSTALL_PREFIX}")
    set(SWITCHBOARD_PC_LIBDIR "${CMAKE_INSTALL_FULL_LIBDIR}")
    set(SWITCHBOARD_PC_INCLUDEDIR "${CMAKE_INSTALL_FULL_INCLUDEDIR}")
else()
    file(RELATIVE_PATH SWITCHBOARD_PC_TO_PREFIX "/${CMAKE_INSTALL_LIBDIR}/pkgconfig" "/")
    set(SWITCHBOARD_PC_PREFIX "\${pcfiledir}/${SWITCHBOARD_PC_TO_PREFIX}")
    set(SWITCHBOARD_PC_LIBDIR "\${prefix}/${CMAKE_INSTALL_LIBDIR}")
    set(SWITCHBOARD_PC_INCLUDEDIR "\${prefix}/${CMAKE_INSTALL_INCLUDEDIR}")
endif()
configure_file(cmake/switchboard-native.pc.in "${CMAKE_CURRENT_BINARY_DIR}/switchboard-native.pc" @ONLY)
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/switchboard-native.pc"
    DESTINATION "${CMAKE_INSTALL_LIBDIR}/pkgconfig"
    COMPONENT Development
)
