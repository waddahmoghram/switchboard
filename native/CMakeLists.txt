cmake_minimum_required(VERSION 3.20)
project(SwitchboardNative VERSION 0.1.0 LANGUAGES C CXX)

option(SWITCHBOARD_BUILD_TESTS "Build authored native tests; running them is a separate step" OFF)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)

find_package(CURL REQUIRED)
find_package(SQLite3 REQUIRED)
find_package(nlohmann_json 3.10 REQUIRED)
find_package(OpenSSL REQUIRED COMPONENTS Crypto)
find_package(Threads REQUIRED)

add_library(switchboard_native SHARED
    src/foundation.cpp
    src/core.cpp
    src/transport.cpp
    src/digest.cpp
    src/home.cpp
    src/devices.cpp
    src/personal.cpp
    src/monitoring.cpp
    src/c_api.cpp
)
target_include_directories(switchboard_native PUBLIC include)
target_link_libraries(switchboard_native
    PRIVATE CURL::libcurl SQLite::SQLite3 OpenSSL::Crypto Threads::Threads
    PUBLIC nlohmann_json::nlohmann_json
)
target_compile_definitions(switchboard_native PRIVATE SWITCHBOARD_NATIVE_BUILD)
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(switchboard_native PRIVATE -Wall -Wextra -Wpedantic)
endif()

add_executable(switchboard-native src/main.cpp)
target_link_libraries(switchboard-native PRIVATE switchboard_native)
add_executable(switchboard-c clients/c/main.c)
target_link_libraries(switchboard-c PRIVATE switchboard_native)
set_target_properties(switchboard-c PROPERTIES LINKER_LANGUAGE CXX)

if(SWITCHBOARD_BUILD_TESTS)
    enable_testing()
    foreach(SUITE IN ITEMS core home devices personal monitoring c_api)
        add_executable(native-test-${SUITE} tests/${SUITE}.cpp)
        target_link_libraries(native-test-${SUITE} PRIVATE switchboard_native)
        add_test(NAME native-${SUITE} COMMAND native-test-${SUITE})
    endforeach()
    add_executable(native-test-c-client tests/c_client.c)
    target_link_libraries(native-test-c-client PRIVATE switchboard_native)
    set_target_properties(native-test-c-client PROPERTIES LINKER_LANGUAGE CXX)
    add_test(NAME native-c-client COMMAND native-test-c-client)
endif()

install(TARGETS switchboard_native switchboard-native switchboard-c)
install(DIRECTORY include/switchboard DESTINATION include)
