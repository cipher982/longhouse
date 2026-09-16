"""The landing page's third-party origins must survive the CSP.

The policy shipped without them once and silently broke the live demo,
analytics, and web fonts on longhouse.ai for three weeks.
"""

from zerg.middleware.security_headers import build_csp


def _directive(csp: str, name: str) -> list[str]:
    for part in csp.split("; "):
        tokens = part.split()
        if tokens and tokens[0] == name:
            return tokens[1:]
    raise AssertionError(f"{name} missing from {csp!r}")


def test_live_demo_sandbox_is_reachable():
    assert "https://freetype-phase1.drose-agents.workers.dev" in _directive(build_csp(), "connect-src")


def test_web_fonts_load():
    csp = build_csp()
    assert {"https://fonts.googleapis.com", "https://api.fontshare.com"} <= set(_directive(csp, "style-src"))
    assert {"https://fonts.gstatic.com", "https://cdn.fontshare.com"} <= set(_directive(csp, "font-src"))


def test_configured_analytics_origin_can_load_and_beacon():
    csp = build_csp(analytics_script_src="https://analytics.drose.io/script.js")
    assert "https://analytics.drose.io" in _directive(csp, "script-src")
    assert "https://analytics.drose.io" in _directive(csp, "connect-src")


def test_no_analytics_configured_allows_no_analytics_host():
    csp = build_csp()
    assert _directive(csp, "script-src") == ["'self'", "https://accounts.google.com/gsi/client"]


def test_malformed_analytics_source_is_ignored():
    csp = build_csp(analytics_script_src="javascript:alert(1)")
    assert _directive(csp, "script-src") == ["'self'", "https://accounts.google.com/gsi/client"]
