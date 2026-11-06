cmake_minimum_required(VERSION 3.16)
project(go2_chassis_offline_tests LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(GO2_CHASSIS_BUILD_OFFLINE_TESTS "Build tests that never contact a robot" ON)

if(GO2_CHASSIS_BUILD_OFFLINE_TESTS)
  find_package(Threads REQUIRED)
  enable_testing()
  add_executable(protocol_guard_test tests/protocol_guard_test.cpp)
  target_include_directories(protocol_guard_test PRIVATE include)
  # These tests intentionally use the standard assert() macro.  Release-like
  # configurations add NDEBUG globally, which would otherwise remove every
  # safety assertion and even make the test fail to compile under -Werror due
  # to the now-unused evidence variables.  Keep assertions active in every
  # build configuration so a green ctest result always exercises the guards.
  target_compile_options(protocol_guard_test PRIVATE
    -UNDEBUG -Wall -Wextra -Wpedantic -Werror)
  add_test(NAME protocol_guard_test COMMAND protocol_guard_test)

  add_executable(runtime_graph_test tests/runtime_graph_test.cpp)
  target_include_directories(runtime_graph_test PRIVATE include)
  target_compile_options(runtime_graph_test PRIVATE
    -UNDEBUG -Wall -Wextra -Wpedantic -Werror)
  add_test(NAME runtime_graph_test COMMAND runtime_graph_test)
  add_executable(native_profile_test tests/native_profile_test.cpp)
  target_include_directories(native_profile_test PRIVATE include)
  target_link_libraries(native_profile_test PRIVATE Threads::Threads)
  target_compile_options(native_profile_test PRIVATE -UNDEBUG -Wall -Wextra -Wpedantic -Werror)
  add_test(NAME native_profile_test COMMAND native_profile_test)
  set_tests_properties(native_profile_test PROPERTIES TIMEOUT 10)
  add_executable(native_state_clock_test tests/native_state_clock_test.cpp)
  target_include_directories(native_state_clock_test PRIVATE include)
  target_compile_options(native_state_clock_test PRIVATE -UNDEBUG -Wall -Wextra -Wpedantic -Werror)
  add_test(NAME native_state_clock_test COMMAND native_state_clock_test)
endif()
