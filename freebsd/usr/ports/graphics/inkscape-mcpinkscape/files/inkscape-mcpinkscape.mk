# Include this fragment from graphics/inkscape/Makefile before bsd.port.mk.
# MCPINKSCAPE_OVERLAY_DIR must name the native/ directory from the exact
# mcpinkscape release source paired with this Inkscape build.

MCPINKSCAPE_OVERLAY_DIR?=  ${.CURDIR}/../mcpinkscape/native
MCPINKSCAPE_BRIDGE_LIB=    ${BUILD_WRKSRC}/libmcpinkscape_bridge.so
MCPINKSCAPE_EXTENSION_DIR= ${STAGEDIR}${PREFIX}/share/inkscape/extensions

CMAKE_ARGS+=    -DCMAKE_PROJECT_TOP_LEVEL_INCLUDES=${MCPINKSCAPE_OVERLAY_DIR}/cmake-overlay.cmake \
                -DMCPINKSCAPE_OVERLAY_DIR=${MCPINKSCAPE_OVERLAY_DIR}

post-build:
	@cd ${BUILD_WRKSRC} && ${SETENVI} ${WRK_ENV} ${MAKE_ENV} \
		${CMAKE_BIN} --build . --target mcpinkscape_bridge

post-install:
	@${MKDIR} ${MCPINKSCAPE_EXTENSION_DIR}
	${INSTALL_LIB} ${MCPINKSCAPE_BRIDGE_LIB} ${MCPINKSCAPE_EXTENSION_DIR}/libmcpinkscape_bridge.so
	${INSTALL_DATA} ${MCPINKSCAPE_OVERLAY_DIR}/mcpinkscape-bridge-start.inx \
		${MCPINKSCAPE_EXTENSION_DIR}/mcpinkscape-bridge-start.inx
	${INSTALL_DATA} ${MCPINKSCAPE_OVERLAY_DIR}/mcpinkscape-bridge-stop.inx \
		${MCPINKSCAPE_EXTENSION_DIR}/mcpinkscape-bridge-stop.inx
	${INSTALL_DATA} ${MCPINKSCAPE_OVERLAY_DIR}/mcpinkscape-bridge-status.inx \
		${MCPINKSCAPE_EXTENSION_DIR}/mcpinkscape-bridge-status.inx
