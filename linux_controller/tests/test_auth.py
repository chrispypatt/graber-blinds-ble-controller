import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from graber.auth import (
    AuthError,
    AuthManager,
    LoginLimiter,
    PasswordStore,
    SessionStore,
    main,
)


PASSWORD = "correct horse battery staple"


def private_file(path, value):
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_password_store_writes_only_scrypt_verifier_mode_0600(tmp_path):
    path = tmp_path / "auth.json"
    store = PasswordStore(path)
    store.set_password("admin", PASSWORD)

    assert path.stat().st_mode & 0o777 == 0o600
    assert PASSWORD not in path.read_text(encoding="utf-8")
    assert store.verify("admin", PASSWORD)
    assert not store.verify("admin", "incorrect password")
    assert not store.verify("someone", PASSWORD)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["algorithm"] == "scrypt"
    assert record["must_change_password"] is False


def test_init_hashes_existing_setup_key_once_and_requires_change(tmp_path):
    setup = private_file(tmp_path / "setup-token", "0123456789abcdef0123456789abcdef\n")
    path = tmp_path / "auth.json"
    store = PasswordStore(path)

    assert store.initialize("admin", setup) is True
    first = path.read_bytes()
    assert store.verify("admin", "0123456789abcdef0123456789abcdef")
    assert store.must_change_password is True
    setup.write_text("a-new-value-that-must-not-replace-auth\n", encoding="utf-8")
    assert store.initialize("admin", setup) is False
    assert path.read_bytes() == first


def test_private_file_permissions_and_symlinks_are_rejected(tmp_path):
    path = tmp_path / "auth.json"
    PasswordStore(path).set_password("admin", PASSWORD)
    path.chmod(0o644)
    with pytest.raises(AuthError, match="0600"):
        PasswordStore(path).verify("admin", PASSWORD)

    source = private_file(tmp_path / "source", "0123456789abcdef")
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(AuthError, match="symlink"):
        PasswordStore(tmp_path / "other").initialize("admin", link)


def test_malformed_scrypt_costs_are_rejected_before_derivation(tmp_path):
    path = tmp_path / "auth.json"
    store = PasswordStore(path)
    store.set_password("admin", PASSWORD)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["r"] = 1_000_000
    path.write_text(json.dumps(record), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(AuthError, match="invalid password file"):
        store.verify("admin", PASSWORD)


def test_sessions_expire_are_bounded_and_store_no_raw_token():
    now = [100.0]
    sessions = SessionStore(ttl_s=60, maximum=2, clock=lambda: now[0])
    first_token, first = sessions.create("admin")
    second_token, _ = sessions.create("admin")
    third_token, _ = sessions.create("admin")
    assert first_token not in sessions._sessions
    assert sessions.get(first_token) is None
    assert sessions.get(second_token) is not None
    assert sessions.get(third_token) is not None
    assert first.expires_at == 160.0
    now[0] = 161.0
    assert sessions.get(second_token) is None


def test_login_limiter_is_per_client_and_windowed():
    now = [0.0]
    limiter = LoginLimiter(attempts=2, window_s=10, clock=lambda: now[0])
    limiter.failed("one")
    limiter.failed("one")
    assert limiter.retry_after("one") == 10
    assert limiter.retry_after("two") == 0
    now[0] = 11
    assert limiter.retry_after("one") == 0


def test_session_and_limiter_state_are_safe_across_worker_threads():
    sessions = SessionStore(ttl_s=60, maximum=512)
    limiter = LoginLimiter(attempts=500, window_s=60)

    def exercise(index):
        token, created = sessions.create(f"user-{index}")
        assert sessions.get(token) == created
        limiter.failed(f"client-{index % 4}")
        limiter.retry_after(f"client-{index % 4}")
        if index % 2:
            sessions.remove(token)

    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(exercise, range(300)))

    assert len(sessions._sessions) <= 300
    assert all(limiter.retry_after(f"client-{index}") == 0 for index in range(4))


def test_bootstrap_login_change_password_invalidates_old_session(tmp_path):
    setup_password = "0123456789abcdef0123456789abcdef"
    setup = private_file(tmp_path / "setup-token", setup_password)
    store = PasswordStore(tmp_path / "auth.json")
    store.initialize("admin", setup)
    manager = AuthManager(store, secure_cookie=False)

    old_token, old_session = manager.login("127.0.0.1", "admin", setup_password)
    assert old_session.must_change_password
    new_token, new_session = manager.change_password(old_session, None, PASSWORD)
    assert manager.sessions.get(old_token) is None
    assert manager.sessions.get(new_token) == new_session
    assert not new_session.must_change_password
    assert store.verify("admin", PASSWORD)


def test_normal_password_change_requires_current_password(tmp_path):
    store = PasswordStore(tmp_path / "auth.json")
    store.set_password("admin", PASSWORD)
    manager = AuthManager(store, secure_cookie=False)
    _, session = manager.login("127.0.0.1", "admin", PASSWORD)
    with pytest.raises(AuthError, match="current"):
        manager.change_password(session, "wrong password", "a different secure password")


def test_set_password_cli_prompts_twice_and_init_never_prints_secret(tmp_path, monkeypatch, capsys):
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr("graber.auth.getpass.getpass", lambda _prompt: next(answers))
    auth_file = tmp_path / "auth.json"
    assert main(["set-password", "--file", str(auth_file)]) == 0
    assert PasswordStore(auth_file).verify("admin", PASSWORD)

    setup = private_file(tmp_path / "setup-token", "0123456789abcdef0123456789abcdef")
    initialized = tmp_path / "initialized.json"
    assert main([
        "init", "--file", str(initialized), "--initial-password-file", str(setup),
    ]) == 0
    output = capsys.readouterr().out
    assert "0123456789abcdef" not in output
    assert PasswordStore(initialized).must_change_password
