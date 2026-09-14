# This file is included by an Inkscape source/ports overlay. It is not a
# standalone build: private Inkscape headers and the exact inkscape_base ABI
# are required.
if(NOT TARGET inkscape_base)
    message(FATAL_ERROR "mcpinkscape_bridge must be built inside the matching Inkscape source tree")
endif()

if(NOT INKSCAPE_VERSION MATCHES "^1\\.4\\.[34]$")
    message(FATAL_ERROR "Unsupported Inkscape ${INKSCAPE_VERSION}; supported source releases are 1.4.3 and 1.4.4")
endif()

get_target_property(_mcpinkscape_base_type inkscape_base TYPE)
if(NOT _mcpinkscape_base_type STREQUAL "SHARED_LIBRARY")
    message(FATAL_ERROR "The native bridge requires BUILD_SHARED_LIBS=ON; a second static Inkscape core would not share GUI state")
endif()

add_library(mcpinkscape_bridge SHARED
    ${CMAKE_CURRENT_LIST_DIR}/src/mcpinkscape_bridge.cpp
)

target_link_libraries(mcpinkscape_bridge PRIVATE inkscape_base)
target_include_directories(mcpinkscape_bridge PRIVATE
    ${CMAKE_SOURCE_DIR}/src
    ${CMAKE_BINARY_DIR}/src
)

# Inkscape 1.5 adds ExecutionEnv to the loaded-effect virtual method. Keep the
# bridge source usable for the 1.4 ports overlay and select the ABI from the
# surrounding Inkscape configure, never from the installed executable.
if(DEFINED INKSCAPE_VERSION AND INKSCAPE_VERSION VERSION_LESS "1.5")
    target_compile_definitions(mcpinkscape_bridge PRIVATE MCPINKSCAPE_EFFECT_API_14=1)
endif()

set_target_properties(mcpinkscape_bridge PROPERTIES
    OUTPUT_NAME mcpinkscape_bridge
    PREFIX lib
)

if(WIN32)
    target_link_libraries(mcpinkscape_bridge PRIVATE ws2_32)
endif()

# Upstream's default RPATH is relative to bin/, whereas this module lives in
# share/inkscape/extensions/. Resolve the sibling core from the installed
# module itself, never from a developer's build directory or environment.
set(_mcpinkscape_extension_dir "${INKSCAPE_SHARE_INSTALL}/extensions")
file(RELATIVE_PATH _mcpinkscape_core_relative
    "${CMAKE_INSTALL_PREFIX}/${_mcpinkscape_extension_dir}"
    "${CMAKE_INSTALL_FULL_LIBDIR}/inkscape")
if(UNIX AND NOT APPLE)
    set_target_properties(mcpinkscape_bridge PROPERTIES
        INSTALL_RPATH "$ORIGIN/${_mcpinkscape_core_relative}")
elseif(APPLE)
    set_target_properties(mcpinkscape_bridge PROPERTIES
        INSTALL_RPATH "@loader_path/${_mcpinkscape_core_relative}")
endif()

install(TARGETS mcpinkscape_bridge
    LIBRARY DESTINATION "${_mcpinkscape_extension_dir}"
    RUNTIME DESTINATION "${_mcpinkscape_extension_dir}"
    COMPONENT inkscape)
install(FILES
    "${CMAKE_CURRENT_LIST_DIR}/mcpinkscape-bridge-start.inx"
    "${CMAKE_CURRENT_LIST_DIR}/mcpinkscape-bridge-stop.inx"
    "${CMAKE_CURRENT_LIST_DIR}/mcpinkscape-bridge-status.inx"
    DESTINATION "${_mcpinkscape_extension_dir}"
    COMPONENT inkscape)

set(_mcpinkscape_build_info "${CMAKE_BINARY_DIR}/mcpinkscape-build-info.txt")
file(WRITE "${_mcpinkscape_build_info}"
    "Inkscape: ${INKSCAPE_VERSION}\nSystem: ${CMAKE_SYSTEM_NAME} ${CMAKE_SYSTEM_PROCESSOR}\n"
    "Compiler: ${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION}\n"
    "Build type: ${CMAKE_BUILD_TYPE}\nCMake: ${CMAKE_VERSION}\n")
foreach(_mcpinkscape_input IN ITEMS CMakeLists.txt cmake-overlay.cmake
        src/mcpinkscape_bridge.cpp src/bridge_json.h
        mcpinkscape-bridge-start.inx mcpinkscape-bridge-stop.inx mcpinkscape-bridge-status.inx)
    file(SHA256 "${CMAKE_CURRENT_LIST_DIR}/${_mcpinkscape_input}" _mcpinkscape_hash)
    file(APPEND "${_mcpinkscape_build_info}" "SHA256 ${_mcpinkscape_input}: ${_mcpinkscape_hash}\n")
endforeach()
install(FILES "${_mcpinkscape_build_info}"
    DESTINATION "${INKSCAPE_SHARE_INSTALL}/mcpinkscape" RENAME build-info.txt COMPONENT inkscape)
install(FILES "${CMAKE_CURRENT_LIST_DIR}/LICENSE.md"
    DESTINATION "${INKSCAPE_SHARE_INSTALL}/mcpinkscape" COMPONENT inkscape)
