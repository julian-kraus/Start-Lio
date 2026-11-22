"""V0 shell, i18n and additive route registry checks."""

import subprocess
import sys
import types
from pathlib import Path

from tracker.lookahead_api import load_routes


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "tracker" / "lookahead_static"


def test_prototype_tokens_and_shell_components_are_present():
    css = (STATIC / "base.css").read_text()
    components = (STATIC / "components.css").read_text()
    assert "--accent:#4d4b9e" in css and "font:400 15px/1.6 Inter" in css
    for name in (".h-md", ".h-sm", ".stat", ".eyebrow", ".mono", ".muted", ".subtle", ".sm", ".xs"):
        assert name in css
    for name in (".card", ".btn-primary", ".btn-secondary", ".btn-text", ".badge", ".count", ".toggle", ".segmented", ".table", ".drawer", ".modal"):
        assert name in components


def test_shell_links_views_and_bilingual_strings():
    page = (ROOT / "tracker" / "lookahead_page.html").read_text()
    strings_source = (STATIC / "strings.js").read_text()
    for view in ("timeline", "plan", "weak", "decisions", "mail", "rules"):
        assert f'data-view="{view}"' in page
    assert 'data-lang="en"' in page and 'data-lang="zh"' in page
    assert 'window.LOOKAHEAD_STRINGS' in strings_source
    assert strings_source.count('"en"') == strings_source.count('"zh"')


def test_route_registry_discovers_each_module(monkeypatch):
    module = types.ModuleType("tracker.lookahead_api_dummy")
    module.ROUTES = [("GET", r"/api/lookahead/dummy", lambda *_: {"ok": True})]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    import tracker.lookahead_api as registry

    monkeypatch.setattr(registry.pkgutil, "iter_modules", lambda path, prefix: [types.SimpleNamespace(name=module.__name__)])
    routes = load_routes()
    assert [(route.method, route.pattern.pattern) for route in routes] == [("GET", r"/api/lookahead/dummy")]


def test_ui_shot_builds_a_headless_command(monkeypatch, tmp_path):
    from ops import ui_shot

    calls = []
    monkeypatch.setattr(ui_shot, "browser", lambda: "/browser")
    monkeypatch.setattr(subprocess, "run", lambda command, check: calls.append((command, check)))
    output = tmp_path / "shots" / "shell.png"
    ui_shot.capture("file:///lookahead.html", output, 1440, 900, 700)
    command, check = calls[0]
    assert check and "--headless=new" in command and "--window-size=1440,900" in command
    assert f"--screenshot={output}" in command
