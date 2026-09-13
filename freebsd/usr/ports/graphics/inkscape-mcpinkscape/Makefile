# This directory is a ports-tree overlay for the native mcpinkscape bridge.
#
# It is deliberately not an independently buildable port: the bridge uses
# private Inkscape C++ headers and must link the inkscape_base target produced
# by the matching graphics/inkscape build.  See README.md and include
# files/inkscape-mcpinkscape.mk from the matching graphics/inkscape port.

PORTNAME=       inkscape-mcpinkscape
DISTVERSION=    0.1.0
CATEGORIES=     graphics

MAINTAINER=     pypipackages01@tspi.at
COMMENT=        Native MCP bridge overlay for an ABI-matched Inkscape build
WWW=            https://inkscape.org

LICENSE=        BSD3CLAUSE

NO_BUILD=       yes
NO_INSTALL=     yes

IGNORE=         must be included as an overlay in graphics/inkscape; see README.md

.include <bsd.port.mk>
