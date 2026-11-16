from pathlib import Path


PACKAGE = Path(__file__).parents[1] / "graber"


def test_browser_ui_never_stores_or_requests_internal_control_token():
    sources = [
        PACKAGE / "static" / "control.js",
        PACKAGE / "static" / "setup.js",
        PACKAGE / "templates" / "index.html",
        PACKAGE / "templates" / "identify.html",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in sources)
    assert "graberControlToken" not in combined
    assert "graberSetupToken" not in combined
    assert "X-Graber-Control-Token" not in combined
    assert "Control token" not in combined
    assert "Setup key" not in combined


def test_login_and_account_forms_use_password_manager_metadata():
    login = (PACKAGE / "templates" / "auth.html").read_text(encoding="utf-8")
    control = (PACKAGE / "templates" / "index.html").read_text(encoding="utf-8")
    setup = (PACKAGE / "templates" / "identify.html").read_text(encoding="utf-8")

    assert 'autocomplete="username"' in login
    assert 'autocomplete="current-password"' in login
    assert 'autocomplete="new-password"' in login
    for page in (control, setup):
        assert 'id="accountButton"' in page
        assert 'autocomplete="current-password"' in page
        assert page.count('autocomplete="new-password"') == 2


def test_mutating_ui_requests_send_csrf_header():
    sources = [
        PACKAGE / "static" / "control.js",
        PACKAGE / "static" / "setup.js",
        PACKAGE / "static" / "auth.js",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in sources)
    assert "X-CSRF-Token" in combined
    assert "/api/auth/logout" in combined
    assert "/api/auth/password" in combined
