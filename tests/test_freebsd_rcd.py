from __future__ import annotations

from pathlib import Path


RCD_SCRIPT = Path(__file__).parents[1] / "freebsd" / "rc.d" / "mcpinkscape"


def test_freebsd_rcd_is_explicitly_opt_in_and_supervises_remote_server():
    source = RCD_SCRIPT.read_text(encoding = "utf-8")
    assert 'mcpinkscape_enable:="NO"' in source
    assert 'mcpinkscape_transport:="remotehttp"' in source
    assert 'required_files="${mcpinkscape_command} ${mcpinkscape_config}"' in source
    assert 'mcpinkscape_daemon_user' in source
    assert 'command="/usr/sbin/daemon"' in source
    assert '--config ${mcpinkscape_config} --transport ${mcpinkscape_transport}' in source
