# This file is included by an Inkscape source/ports overlay. It is not a
# standalone build: private Inkscape headers and the exact inkscape_base ABI
# are required.
if(NOT TARGET inkscape_base)
    message(FATAL_ERROR "mcpinkscape_bridge must be built inside the matching Inkscape source tree")
endif()

add_library(mcpinkscape_bridge SHARED EXCLUDE_FROM_ALL
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
