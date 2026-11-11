import pytest

from ui import accounts, app as grid_app, auth, oidc, recorder, store


@pytest.fixture(autouse=True)
def _fresh_sign_in_limit():
    """Wrong admin tokens in one test must not count towards another test's guessing limit, its sign-in starts
    towards another's start limit, nor its throttled audit rows keep another's from being written."""
    auth.reset_failures()
    grid_app.reset_login_starts()
    grid_app.reset_audit_throttles()
    yield
    auth.reset_failures()
    grid_app.reset_login_starts()
    grid_app.reset_audit_throttles()


@pytest.fixture(autouse=True)
def _fresh_oidc_caches():
    """One test's discovery document, signing keys and table check must not answer for another test's identity
    provider or database, nor the warnings it logged keep another's from being logged."""
    oidc.reset_caches()
    accounts.reset_caches()
    store.reset_warnings()
    yield
    oidc.reset_caches()
    accounts.reset_caches()
    store.reset_warnings()


@pytest.fixture(autouse=True)
def _recorders_stop_with_their_test(monkeypatch):
    """Every status poll starts the app's background recorder. Left running, it reads whatever database the
    next tests point store at, and races their setup (it applies the schema to a MySQL database they have
    just emptied, for one). Stop each one a test started when the test ends."""
    started = set()
    ensure_running = recorder.Recorder.ensure_running

    def tracked(rec):
        started.add(rec)
        ensure_running(rec)

    monkeypatch.setattr(recorder.Recorder, "ensure_running", tracked)
    yield
    for rec in started:
        rec.stop()


# What a platform puts in the environment about its managed identity, and the settings that use one or a token file.
PLATFORM_VARIABLES = ("AZURE_FEDERATED_TOKEN_FILE", "GRID_OIDC_FEDERATED_TOKEN_FILE", "GRID_OIDC_MANAGED_IDENTITY",
                      "IDENTITY_ENDPOINT", "IDENTITY_HEADER", "IMDS_ENDPOINT", "IDENTITY_SERVER_THUMBPRINT",
                      "MSI_ENDPOINT", "MSI_SECRET", "WEBSITE_SITE_NAME", "CONTAINER_APP_NAME", "KUBERNETES_SERVICE_HOST")


@pytest.fixture(scope="session")
def _imds_guard():
    """A recorder on loopback where the VM's managed identity address would be. CI may run on an Azure VM, whose
    real 169.254.169.254 would answer: no test may ever ask it, and one that wrongly does is heard here."""
    from ui.tests import fake_idp

    recorder = fake_idp.serve_recorder()
    yield recorder
    recorder.stop()


@pytest.fixture(autouse=True)
def _no_platform_token(monkeypatch, _imds_guard):
    """The settings of a test are the ones it sets: not a token file or a managed identity the machine running the
    tests was given. oidc.IMDS_URL points at the guard, and the test fails if anything asked it; a test that uses
    the Instance Metadata Service points IMDS_URL at its own fake after this."""
    for name in PLATFORM_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(oidc, "IMDS_URL", _imds_guard.url + "/metadata/identity/oauth2/token")
    oidc.reset_caches()
    before = _imds_guard.count
    yield
    assert _imds_guard.count == before, "a request went to the VM's managed identity address: {}".format(
        _imds_guard.seen[before:])
