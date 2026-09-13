# Include this through CMAKE_PROJECT_TOP_LEVEL_INCLUDES when configuring an
# Inkscape source tree.  Deferral lets the upstream tree define inkscape_base
# first, without modifying its CMakeLists.txt or copying this overlay into it.
if(NOT DEFINED MCPINKSCAPE_OVERLAY_DIR)
    message(FATAL_ERROR "set MCPINKSCAPE_OVERLAY_DIR to the mcpInkscape native directory")
endif()

if(NOT IS_DIRECTORY "${MCPINKSCAPE_OVERLAY_DIR}")
    message(FATAL_ERROR "MCPINKSCAPE_OVERLAY_DIR is not a directory: ${MCPINKSCAPE_OVERLAY_DIR}")
endif()

# CMake does not permit add_subdirectory() from a deferred call. Including the
# target file works here because its source path is explicitly based on its own
# CMAKE_CURRENT_LIST_DIR.
cmake_language(DEFER DIRECTORY "${CMAKE_SOURCE_DIR}" CALL include
    "${MCPINKSCAPE_OVERLAY_DIR}/CMakeLists.txt"
)
