cmake_minimum_required(VERSION 3.21)
project(MemoryOptimizer VERSION 1.0.0 LANGUAGES CXX RC)

if(NOT WIN32 OR NOT MSVC OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8 OR CMAKE_CXX_COMPILER_ARCHITECTURE_ID MATCHES "ARM")
    message(FATAL_ERROR "Use an x64 MSVC generator for this Windows application.")
endif()

add_library(project_options INTERFACE)
target_compile_features(project_options INTERFACE cxx_std_17)
target_compile_definitions(project_options INTERFACE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX _WIN32_WINNT=0x0A00)
target_compile_options(project_options INTERFACE $<$<COMPILE_LANGUAGE:CXX>:/W4;/utf-8;/permissive->)

add_library(memory_core STATIC src/policy.cpp src/windows_memory.cpp)
set_property(TARGET memory_core PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
target_link_libraries(memory_core PUBLIC project_options psapi)

function(add_desktop_application name entrypoint)
    add_executable(${name} WIN32 ${entrypoint} src/main.cpp resources/app.rc)
    target_include_directories(${name} PRIVATE src)
    target_link_libraries(${name} PRIVATE memory_core user32 gdi32 shell32 comctl32)
    target_link_options(${name} PRIVATE "/MANIFEST:NO")
    set_property(TARGET ${name} PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endfunction()

add_desktop_application(MemoryOptimizer src/entry.cpp)

include(CTest)

if(BUILD_TESTING)
    add_desktop_application(memory_gui_fixture tests/gui_fixture.cpp)

    add_executable(memory_core_tests tests/core_tests.cpp)
    set_property(TARGET memory_core_tests PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
    target_include_directories(memory_core_tests PRIVATE src)
    target_link_libraries(memory_core_tests PRIVATE memory_core)
    add_test(NAME memory-core COMMAND memory_core_tests)

    add_executable(memory_working_set_test tests/working_set_test.cpp)
    set_property(TARGET memory_working_set_test PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
    target_include_directories(memory_working_set_test PRIVATE src)
    target_link_libraries(memory_working_set_test PRIVATE memory_core)
    add_test(NAME memory-working-set COMMAND memory_working_set_test)
    set_tests_properties(memory-working-set PROPERTIES TIMEOUT 20)
endif()
