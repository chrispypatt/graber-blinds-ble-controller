import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from graber.auth import AuthError, AuthManager, LoginLimiter, PasswordStore, SessionStore, main


RECOVERY_PASSWORD = "0123456789abcdef0123456789abcdef"
OWNER_PASSWORD = "correct horse battery staple"


def private_file(path, value):
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def initialized_store(tmp_path, *, owner=True):
    setup = private_file(tmp_path / "setup-token", RECOVERY_PASSWORD + "\n")
    store = PasswordStore(tmp_path / "auth.json")
    assert store.initialize("admin", setup)
    if owner:
        store.set_owner("owner", OWNER_PASSWORD)
    return store, setup


def test_init_creates_v2_recovery_without_plaintext(tmp_path):
    store, _ = initialized_store(tmp_path, owner=False)
    record = json.loads(store.path.read_text(encoding="utf-8"))
    assert record["version"] == 2
    assert record["owner"] is None
    assert record["recovery"]["username"] == "admin"
    assert RECOVERY_PASSWORD not in store.path.read_text(encoding="utf-8")
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.authenticate("ADMIN", RECOVERY_PASSWORD) == ("recovery", "admin")
    assert store.setup_required


def test_init_is_idempotent_and_does_not_rotate_recovery(tmp_path):
    store, setup = initialized_store(tmp_path, owner=False)
    original = store.path.read_bytes()
    setup.write_text("a-new-setup-token-that-must-not-rotate-recovery\n", encoding="utf-8")
    assert store.initialize("admin", setup) is False
    assert store.path.read_bytes() == original
    assert store.verify("admin", RECOVERY_PASSWORD)


def test_owner_is_separate_and_admin_is_reserved_case_insensitively(tmp_path):
    store, _ = initialized_store(tmp_path, owner=False)
    for reserved in ("admin", "Admin", "ADMIN"):
        with pytest.raises(AuthError, match="reserved"):
            store.set_owner(reserved, OWNER_PASSWORD)
    store.set_owner("chris", OWNER_PASSWORD)
    assert store.authenticate("CHRIS", OWNER_PASSWORD) == ("owner", "chris")
    assert store.verify("admin", RECOVERY_PASSWORD)
    assert not store.setup_required


def test_owner_password_change_never_replaces_recovery(tmp_path):
    store, _ = initialized_store(tmp_path)
    store.change_owner_password("owner", OWNER_PASSWORD, "a different secure password")
    assert store.verify("owner", "a different secure password")
    assert not store.verify("owner", OWNER_PASSWORD)
    assert store.verify("admin", RECOVERY_PASSWORD)


def test_reset_owner_returns_to_setup_without_removing_recovery(tmp_path):
    store, _ = initialized_store(tmp_path)
    store.reset_owner()
    assert store.setup_required
    assert store.owner_username is None
    assert store.verify("admin", RECOVERY_PASSWORD)


def legacy_record(username, password, *, must_change):
    credential = PasswordStore._credential(username, password)
    return {"version": 1, "algorithm": "scrypt", **credential,
            "must_change_password": must_change}


def test_v1_migration_preserves_changed_password_as_owner_and_adds_recovery(tmp_path):
    path = private_file(
        tmp_path / "auth.json",
        json.dumps(legacy_record("admin", OWNER_PASSWORD, must_change=False)),
    )
    setup = private_file(tmp_path / "setup-token", RECOVERY_PASSWORD)
    store = PasswordStore(path)
    assert store.initialize("admin", setup)
    assert store.verify("admin", RECOVERY_PASSWORD)
    assert store.verify("owner", OWNER_PASSWORD)
    assert store.owner_username == "owner"
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2


def test_v1_bootstrap_migration_requires_owner_setup(tmp_path):
    path = private_file(
        tmp_path / "auth.json",
        json.dumps(legacy_record("admin", RECOVERY_PASSWORD, must_change=True)),
    )
    setup = private_file(tmp_path / "setup-token", RECOVERY_PASSWORD)
    store = PasswordStore(path)
    assert store.initialize("admin", setup)
    assert store.setup_required
    assert store.verify("admin", RECOVERY_PASSWORD)


def test_v1_setup_token_is_not_promoted_to_owner_when_flag_was_cleared(tmp_path):
    path = private_file(
        tmp_path / "auth.json",
        json.dumps(legacy_record("admin", RECOVERY_PASSWORD, must_change=False)),
    )
    setup = private_file(tmp_path / "setup-token", RECOVERY_PASSWORD)
    store = PasswordStore(path)
    assert store.initialize("admin", setup)
    assert store.setup_required
    assert store.owner_username is None
    assert store.verify("admin", RECOVERY_PASSWORD)


def test_private_permissions_symlinks_and_malformed_costs_are_rejected(tmp_path):
    store, _ = initialized_store(tmp_path)
    store.path.chmod(0o644)
    with pytest.raises(AuthError, match="0600"):
        store.verify("admin", RECOVERY_PASSWORD)
    store.path.chmod(0o600)
    record = json.loads(store.path.read_text(encoding="utf-8"))
    record["recovery"]["r"] = 1_000_000
    store.path.write_text(json.dumps(record), encoding="utf-8")
    store.path.chmod(0o600)
    with pytest.raises(AuthError, match="invalid credential"):
        store.verify("admin", RECOVERY_PASSWORD)
    source = private_file(tmp_path / "source", RECOVERY_PASSWORD)
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(AuthError, match="symlink"):
        PasswordStore(tmp_path / "other").initialize("admin", link)


def test_sessions_expire_are_bounded_and_store_no_raw_token():
    now = [100.0]
    sessions = SessionStore(ttl_s=60, maximum=2, clock=lambda: now[0])
    first_token, first = sessions.create("owner")
    second_token, _ = sessions.create("owner")
    third_token, _ = sessions.create("owner")
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


def test_fresh_admin_session_creates_owner_and_both_accounts_remain_valid(tmp_path):
    store, _ = initialized_store(tmp_path, owner=False)
    manager = AuthManager(store, secure_cookie=False)
    _, admin = manager.login("one", "admin", RECOVERY_PASSWORD)
    assert admin.role == "recovery" and admin.setup_required
    _, owner = manager.configure_owner(admin, "chris", OWNER_PASSWORD)
    assert owner.role == "owner" and not owner.setup_required
    assert manager.login("two", "admin", RECOVERY_PASSWORD)[1].role == "recovery"
    assert manager.login("three", "chris", OWNER_PASSWORD)[1].role == "owner"


def test_admin_replaces_owner_and_stale_owner_cannot_change_password(tmp_path):
    store, _ = initialized_store(tmp_path)
    manager = AuthManager(store, secure_cookie=False)
    _, stale_owner = manager.login("one", "owner", OWNER_PASSWORD)
    _, admin = manager.login("two", "admin", RECOVERY_PASSWORD)
    _, replacement = manager.configure_owner(admin, "new_owner", "replacement secure password")
    assert replacement.username == "new_owner"
    assert manager.login("three", "owner", OWNER_PASSWORD) is None
    assert manager.login("four", "new_owner", "replacement secure password") is not None
    assert manager.login("five", "admin", RECOVERY_PASSWORD) is not None
    with pytest.raises(AuthError, match="credentials changed"):
        manager.change_password(stale_owner, OWNER_PASSWORD, "another secure password")


def test_recovery_cannot_be_changed_through_browser_manager(tmp_path):
    store, _ = initialized_store(tmp_path)
    manager = AuthManager(store, secure_cookie=False)
    _, admin = manager.login("one", "admin", RECOVERY_PASSWORD)
    with pytest.raises(AuthError, match="command line"):
        manager.change_password(admin, RECOVERY_PASSWORD, "new recovery password")


def test_concurrent_owner_replacement_accepts_only_one_generation(tmp_path):
    store, _ = initialized_store(tmp_path)
    manager = AuthManager(store, secure_cookie=False)
    _, admin = manager.login("one", "admin", RECOVERY_PASSWORD)
    def replace(index):
        try:
            return manager.configure_owner(admin, f"owner_{index}", f"secure replacement {index}")
        except AuthError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(replace, range(2)))
    assert sum(result is not None for result in results) == 1
    assert manager.login("recovery", "admin", RECOVERY_PASSWORD) is not None


def test_cli_init_recovery_rotation_and_owner_reset(tmp_path, monkeypatch, capsys):
    setup = private_file(tmp_path / "setup-token", RECOVERY_PASSWORD)
    auth_file = tmp_path / "auth.json"
    assert main(["init", "--file", str(auth_file), "--initial-password-file", str(setup)]) == 0
    store = PasswordStore(auth_file)
    store.set_owner("owner", OWNER_PASSWORD)
    answers = iter(["new recovery password", "new recovery password"])
    monkeypatch.setattr("graber.auth.getpass.getpass", lambda _prompt: next(answers))
    assert main(["set-recovery-password", "--file", str(auth_file)]) == 0
    assert store.verify("admin", "new recovery password")
    assert main(["reset-owner", "--file", str(auth_file)]) == 0
    assert store.setup_required
    assert RECOVERY_PASSWORD not in capsys.readouterr().out


def test_persisted_owner_session_survives_restart_and_logout_revokes_it(tmp_path):
    store, _ = initialized_store(tmp_path)
    path = tmp_path / "sessions.json"
    token, session = AuthManager(store, session_path=path).login("client", "owner", OWNER_PASSWORD)
    restarted = AuthManager(store, session_path=path)
    assert restarted.get_session(token) == session
    assert token not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    restarted.sessions.remove(token)
    assert AuthManager(store, session_path=path).get_session(token) is None


def test_persistent_sessions_reject_external_password_changes_and_keep_new_login(tmp_path):
    store, _ = initialized_store(tmp_path)
    path = tmp_path / "sessions.json"
    manager = AuthManager(store, session_path=path)
    old, _ = manager.login("client", "owner", OWNER_PASSWORD)
    store.set_owner("owner", "new strong password")
    assert manager.get_session(old) is None
    restarted = AuthManager(store, session_path=path)
    assert restarted.get_session(old) is None
    token, session = restarted.login("client", "owner", "new strong password")
    replacement, _ = restarted.change_password(session, "new strong password", OWNER_PASSWORD)
    again = AuthManager(store, session_path=path)
    assert again.get_session(token) is None
    assert again.get_session(replacement) is not None


def test_persisted_sessions_keep_original_expiry(tmp_path):
    now = [100.0]
    path = tmp_path / "sessions.json"
    sessions = SessionStore(ttl_s=60, clock=lambda: now[0], path=path)
    token, _ = sessions.create("owner", credential_revision="test")
    now[0] = 140
    assert SessionStore(ttl_s=60, clock=lambda: now[0], path=path).get(token).expires_at == 160
    now[0] = 161
    assert SessionStore(ttl_s=60, clock=lambda: now[0], path=path).get(token) is None
