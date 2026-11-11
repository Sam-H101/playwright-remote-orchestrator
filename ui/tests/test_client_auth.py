"""How the Grid UI proves to the identity provider that it is the client: a client secret, or a token the platform
puts in a file (a Kubernetes cluster's, or any issuer's), sent as a client assertion. The fake provider verifies
that token as Entra verifies one against a federated identity credential. A managed identity's token is in
test_managed_identity.py.
Run from the repo root: python -m pytest ui/tests"""
import base64
import datetime
import inspect
import io
import ipaddress
import json
import logging
import os
import re
import socket
import time
import types
import urllib.error
import urllib.parse
import urllib.request

import pytest

from ui import app as grid_app, oidc, permissions
from ui.tests import fake_idp
from ui.tests.test_accounts import add_role, db, make_user, run, sign_in_as  # noqa: F401  (db is a fixture)
from ui.tests.test_oidc import (ADMIN, CALLBACK, GOOD, SUBJECT, code_of, configure, cookie_set, count,  # noqa: F401
                                grid, idp, local, sign_in, signin_code, start, stopped_clock, through)

P1 = "GRID_OIDC_CLIENT_AUTH is auto, basic, post or federated."
P2 = ("Set only one of GRID_OIDC_CLIENT_SECRET, GRID_OIDC_FEDERATED_TOKEN_FILE and GRID_OIDC_MANAGED_IDENTITY: "
      "GRID_OIDC_CLIENT_SECRET and GRID_OIDC_FEDERATED_TOKEN_FILE are set, and the Grid UI won't pick one.")
P3 = ("Set GRID_OIDC_CLIENT_SECRET, or GRID_OIDC_CLIENT_AUTH=federated to use a token the platform issues, so the "
      "Grid UI can prove to the identity provider that it is the client.")
P4 = "GRID_OIDC_CLIENT_AUTH=federated doesn't go with GRID_OIDC_CLIENT_SECRET, which is set: unset one of them."
P5 = "GRID_OIDC_CLIENT_AUTH={} needs GRID_OIDC_CLIENT_SECRET."
P5_FEDERATED = ("GRID_OIDC_CLIENT_AUTH=federated needs a token to send: GRID_OIDC_FEDERATED_TOKEN_FILE, "
                "GRID_OIDC_MANAGED_IDENTITY, or the AZURE_FEDERATED_TOKEN_FILE that Azure workload identity sets in a "
                "Kubernetes pod.")
P6 = ("GRID_OIDC_FEDERATED_TOKEN_FILE is used only with GRID_OIDC_CLIENT_AUTH=federated: set that, or unset "
      "GRID_OIDC_FEDERATED_TOKEN_FILE.")
HTTPS_ISSUER = "GRID_OIDC_ISSUER must be an https URL (or set GRID_OIDC_ALLOW_HTTP=1 for development)."
NO_ASSERTIONS = ("the identity provider doesn't take client assertions: private_key_jwt isn't among the "
                 "token_endpoint_auth_methods_supported its discovery document lists")
PLAIN_HTTP = ("the identity provider's token_endpoint is plain http on a host that isn't loopback, and the platform's "
              "token goes there only with GRID_OIDC_ALLOW_HTTP=1")  # X1
W1 = ("GRID_OIDC_ALLOW_HTTP=1 and GRID_OIDC_CLIENT_AUTH=federated: the platform's token goes to the identity provider "
      "over plain http, where anyone on the network between them can read it and use it as the Grid UI's credential "
      "until it expires. For development only.")
GENERAL_HTTP = "GRID_OIDC_ALLOW_HTTP=1: single sign-on runs over plain http, which is for development only."
REFUSED = "the identity provider's token endpoint refused a sign-in's code: "
SUBJECT_HINT = ("AADSTS700213: no federated identity credential on the app registration names the token's subject. It "
                "must be the token's subject letter for letter: system:serviceaccount:<namespace>:<service account> "
                "for a Kubernetes token, the object (principal) id for a managed identity (the Admin page's Single "
                "sign-on tab shows it).")
TIME_HINT = ("AADSTS700024: the platform's token was outside its valid time. Check this machine's clock, and that the "
             "platform still gives out fresh tokens.")
FEDERATED_HINT = ("The provider's federated identity credential must name the token's issuer, subject and audience "
                  "exactly: the Admin page's Single sign-on tab shows them.")
MANAGED_IDENTITY_HINT = ("The app registration's federated identity credential must name GRID_OIDC_ISSUER, the managed "
                         "identity's principal id and the audience of GRID_OIDC_ISSUER's cloud "
                         "(api://AzureADTokenExchange in the public cloud), whatever the token itself states: the Admin "
                         "page's Single sign-on tab shows them as Credential issuer, Credential subject and Credential "
                         "audience.")
SECRET_HINT = ("Check GRID_OIDC_CLIENT_SECRET, and set GRID_OIDC_CLIENT_AUTH when the client is registered for one "
               "method only.")
OURS, THEIRS = "GRID_OIDC_FEDERATED_TOKEN_FILE", "AZURE_FEDERATED_TOKEN_FILE"
SERVICE_ACCOUNT, AUDIENCE = "system:serviceaccount:grid:grid-ui", "api://AzureADTokenExchange"
ALL_METHODS = ["client_secret_basic", "client_secret_post", "private_key_jwt"]
ADMIN_TOKEN = {"X-Grid-Token": ADMIN}


def write_token(path, cluster, **changes):
    """Puts a token of the cluster's in the file, as the platform does (no newline after it). Returns the token."""
    token = cluster.token(**changes)
    path.write_bytes(token.encode())
    return token


def raw_token(path, claims):
    """Puts an unsigned token in the file whose payload is claims (a str: that text as written, which need not be
    JSON). Returns the token."""
    payload = claims if isinstance(claims, str) else json.dumps(claims)
    token = ".".join([fake_idp._segment({"alg": "RS256", "typ": "JWT"}), fake_idp._b64(payload.encode()),
                      fake_idp._b64(b"not a signature")])
    path.write_bytes(token.encode())
    return token


def trusting(provider, monkeypatch, tmp_path):
    """The provider takes client assertions and trusts a new cluster, whose token is in a file the settings name
    with GRID_OIDC_CLIENT_AUTH=federated and no client secret. The provider gains cluster, token_file and token."""
    provider.cluster = fake_idp.FakeCluster()
    provider.auth_methods = list(ALL_METHODS)
    provider.trust(provider.cluster)
    provider.token_file = tmp_path / "platform-token"
    provider.token = write_token(provider.token_file, provider.cluster)
    monkeypatch.delenv("GRID_OIDC_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", "federated")
    monkeypatch.setenv(OURS, str(provider.token_file))
    return provider


@pytest.fixture
def cluster_idp(idp, monkeypatch, tmp_path):
    """The fake identity provider, set up as trusting() says. (After grid or db in a test's arguments: db clears
    the settings.)"""
    return trusting(idp, monkeypatch, tmp_path)


def platform_names(monkeypatch, path, name):
    """The token file is named by that setting and no other."""
    for setting in (OURS, THEIRS):
        monkeypatch.delenv(setting, raising=False)
    monkeypatch.setenv(name, str(path))


def opened_files(monkeypatch):
    """The paths oidc reads a small file from, from now on, as a list that grows."""
    opened, small_file = [], oidc._small_file

    def counted(path, limit):
        opened.append(path)
        return small_file(path, limit)

    monkeypatch.setattr(oidc, "_small_file", counted)
    return opened


def token_requests(monkeypatch, answer=None):
    """Every POST oidc sends from now on, as a list of (url, form, headers) that grows. With answer: POSTs aren't
    sent, and are answered that."""
    requests, fetch = [], oidc._fetch

    def recorded(url, data=None, headers=None, **kwargs):
        if data is None:
            return fetch(url, **kwargs)
        requests.append((url, dict(urllib.parse.parse_qsl(data.decode(), keep_blank_values=True)), dict(headers)))
        return answer if answer is not None else fetch(url, data=data, headers=headers, **kwargs)

    monkeypatch.setattr(oidc, "_fetch", recorded)
    return requests


def session_problems(client):
    """What /api/session tells the admin token is wrong with each way of signing in."""
    return [p.get("problem") for p in client.get("/api/session", headers=ADMIN_TOKEN).get_json()["providers"]]


# ---- which method ----------------------------------------------------------------------

LOOPBACK = {"GRID_OIDC_ISSUER": "http://127.0.0.1:8080"}
PLAIN = {"GRID_OIDC_ISSUER": "http://login.example.com"}
ALLOWED = {"GRID_OIDC_ALLOW_HTTP": "1"}


# S: a client secret; F: GRID_OIDC_FEDERATED_TOKEN_FILE; Z: AZURE_FEDERATED_TOKEN_FILE, which the platform sets.
@pytest.mark.parametrize("setting, secret, ours, theirs, more, method, problem", [
    ("auto", True, True, False, {}, None, P2),
    ("auto", True, True, True, {}, None, P2),
    ("basic", True, True, False, {}, None, P2),
    ("post", True, True, True, {}, None, P2),
    ("federated", True, True, False, {}, None, P2),
    ("federated", True, True, True, {}, None, P2),
    (None, True, False, False, {}, "secret", None),
    ("auto", True, False, False, {}, "secret", None),
    ("auto", True, False, True, {}, "secret", None),
    ("auto", False, False, False, {}, None, P3),
    ("auto", False, False, True, {}, None, P3),
    (None, False, False, True, {}, None, P3),
    ("auto", False, True, False, {}, None, P6),
    ("auto", False, True, True, {}, None, P6),
    ("basic", True, False, False, {}, "basic", None),
    ("basic", True, False, True, {}, "basic", None),
    ("post", True, False, False, {}, "post", None),
    ("post", True, False, True, {}, "post", None),
    ("basic", False, False, False, {}, None, P5.format("basic")),
    ("basic", False, False, True, {}, None, P5.format("basic")),
    ("post", False, False, False, {}, None, P5.format("post")),
    ("post", False, False, True, {}, None, P5.format("post")),
    ("basic", False, True, False, {}, None, P6),
    ("post", False, True, True, {}, None, P6),
    ("federated", False, True, False, {}, "federated", None),
    ("federated", False, True, True, {}, "federated", None),
    ("federated", False, False, True, {}, "federated", None),
    ("FEDERATED", False, False, True, {}, "federated", None),
    ("federated", False, False, False, {}, None, P5_FEDERATED),
    ("federated", True, False, False, {}, None, P4),
    ("federated", True, False, True, {}, None, P4),
    ("federated", False, True, False, dict(PLAIN, **ALLOWED), "federated", None),
    ("federated", False, False, True, dict(PLAIN, **ALLOWED), "federated", None),
    ("federated", False, True, False, dict(LOOPBACK, **ALLOWED), "federated", None),
    ("federated", False, True, False, LOOPBACK, "federated", None),
    ("jwt", True, False, False, {}, None, P1),
    ("private_key_jwt", False, True, True, {}, None, P1),
    ("federate", False, False, True, {}, None, P1),
    ("secret", True, False, False, {}, None, P1),
])
def test_what_each_setting_of_the_method_comes_to(monkeypatch, tmp_path, setting, secret, ours, theirs, more, method,
                                                  problem):
    usable = tmp_path / "token"
    write_token(usable, fake_idp.FakeCluster())
    changes = {"GRID_OIDC_CLIENT_AUTH": setting, "GRID_OIDC_CLIENT_SECRET": GOOD["GRID_OIDC_CLIENT_SECRET"] if secret
               else None, OURS: str(usable) if ours else None, THEIRS: str(usable) if theirs else None}
    configure(monkeypatch, **dict(changes, **more))
    assert oidc._client_method(oidc.settings()) == (method, problem)
    assert oidc.client_method() == method
    assert oidc.problem() == problem
    assert oidc.configured()


@pytest.mark.parametrize("secret, ours, theirs", [(False, True, False), (False, False, True), (True, False, False)])
def test_an_http_issuer_is_the_general_https_problem(monkeypatch, tmp_path, secret, ours, theirs):
    """Without GRID_OIDC_ALLOW_HTTP it is the sentence every method meets; the method itself is still decided."""
    usable = tmp_path / "token"
    write_token(usable, fake_idp.FakeCluster())
    configure(monkeypatch, GRID_OIDC_CLIENT_AUTH=None if secret else "federated",
              GRID_OIDC_CLIENT_SECRET=GOOD["GRID_OIDC_CLIENT_SECRET"] if secret else None,
              **dict(PLAIN, **{OURS: str(usable) if ours else None, THEIRS: str(usable) if theirs else None}))
    method = "secret" if secret else "federated"
    assert oidc._client_method(oidc.settings()) == (method, None) and oidc.client_method() == method
    assert oidc.problem() == HTTPS_ISSUER


def test_the_method_is_decided_without_opening_a_file(monkeypatch, tmp_path):
    def never(*args, **kwargs):
        raise AssertionError("a file was looked at, or an endpoint asked")

    decided = []
    for changes in ({"GRID_OIDC_CLIENT_SECRET": None, "GRID_OIDC_CLIENT_AUTH": "federated", OURS: str(tmp_path / "t")},
                    {"GRID_OIDC_CLIENT_SECRET": None, "GRID_OIDC_CLIENT_AUTH": "federated", THEIRS: str(tmp_path / "t")},
                    {THEIRS: str(tmp_path / "t")}, {OURS: str(tmp_path / "t")},
                    {"GRID_OIDC_CLIENT_SECRET": None, THEIRS: str(tmp_path / "t")},
                    {"GRID_OIDC_CLIENT_SECRET": None, "GRID_OIDC_CLIENT_AUTH": "federated",
                     "GRID_OIDC_ISSUER": fake_idp.TENANT_ISSUER, "GRID_OIDC_MANAGED_IDENTITY": fake_idp.CLIENT_ID_MI}):
        configure(monkeypatch, **changes)
        with monkeypatch.context() as patched:
            patched.setattr("builtins.open", never)
            patched.setattr(os, "stat", never)
            patched.setattr(oidc, "_identity_fetch", never)
            patched.setattr(oidc.urllib.request, "build_opener", never)
            found = (oidc._client_method(oidc.settings()), oidc.client_method(), oidc.platform_token_unused())
        decided.append(found)
    assert decided == [(("federated", None), "federated", False), (("federated", None), "federated", False),
                       (("secret", None), "secret", True), ((None, P2), None, False), ((None, P3), None, True),
                       (("federated", None), "federated", False)]


@pytest.mark.parametrize("setting, problem", [("auto", P3), ("basic", P5.format("basic")), ("post", P5.format("post"))])
def test_auto_never_sends_the_platforms_token(grid, db, cluster_idp, monkeypatch, setting, problem):
    """Only GRID_OIDC_CLIENT_AUTH=federated sends it: a pod the platform gave a token, with no secret and any other
    method, is as unconfigured as one without, and the token stays in its file."""
    provider = cluster_idp
    platform_names(monkeypatch, provider.token_file, THEIRS)
    monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", setting)
    assert oidc.problem() == problem and oidc.client_method() is None
    r = start(grid)
    assert signin_code(r) == "misconfigured" and cookie_set(r, "grid-login") is None
    assert count(db, "oidcLogins") == 0
    # A sign-in on its way when the setting changes back: its callback sends nothing either.
    monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", "federated")
    r = start(grid)
    assert r.status_code == 303 and r.headers["Location"].startswith(provider.base_url + "/authorize?")
    callback = provider.approve(r.headers["Location"])
    monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", setting)
    opened = opened_files(monkeypatch)
    r = grid.get(local(callback))
    assert signin_code(r) == "misconfigured"
    assert provider.counts["token"] == 0 and provider.assertions == [] and provider.auth_used == []
    assert cookie_set(r, "grid-session") is None and count(db, "userSessions") == 0
    assert opened == []


@pytest.mark.parametrize("setting, used", [("auto", "client_secret_basic"), ("basic", "client_secret_basic"),
                                           ("post", "client_secret_post")])
def test_the_platforms_file_never_overrides_a_secret(idp, monkeypatch, tmp_path, setting, used):
    cluster = fake_idp.FakeCluster()
    idp.auth_methods = list(ALL_METHODS)
    idp.trust(cluster)
    write_token(tmp_path / "token", cluster)
    monkeypatch.setenv(THEIRS, str(tmp_path / "token"))
    monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", setting)
    opened, requests = opened_files(monkeypatch), token_requests(monkeypatch)
    assert oidc.problem() is None and oidc.platform_token_unused() is True
    assert oidc.verify_id_token(*sign_in(idp))["sub"] == SUBJECT
    assert idp.auth_used == [used] and idp.assertions == [] and opened == []
    ((_, form, _),) = requests
    assert "client_assertion" not in form and "client_assertion_type" not in form


def test_the_platforms_file_alone_turns_nothing_on(monkeypatch, tmp_path):
    write_token(tmp_path / "token", fake_idp.FakeCluster())
    configure(monkeypatch, GRID_OIDC_ISSUER=None, GRID_OIDC_CLIENT_ID=None, GRID_OIDC_CLIENT_SECRET=None,
              **{THEIRS: str(tmp_path / "token")})
    # What workload identity sets besides the file: none of it is read.
    monkeypatch.setenv("AZURE_CLIENT_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("AZURE_TENANT_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com/")
    opened = opened_files(monkeypatch)
    s = oidc.settings()
    assert not oidc.configured() and (s.issuer, s.client_id, s.client_auth) == ("", "", "auto")
    assert oidc.platform_token_unused() is False and oidc.client_method() is None
    assert oidc.problem() is None and oidc.federated_token_facts() is None and oidc.provider_problem() is None
    assert opened == []
    for name in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_AUTHORITY_HOST"):
        assert name not in inspect.getsource(oidc) and name not in inspect.getsource(grid_app)


def test_an_empty_setting_is_unset(monkeypatch):
    configure(monkeypatch, **{OURS: "", THEIRS: " "})
    assert oidc.problem() is None and oidc.client_method() == "secret" and oidc.platform_token_unused() is False
    configure(monkeypatch, GRID_OIDC_CLIENT_SECRET=None, GRID_OIDC_CLIENT_AUTH="federated", **{OURS: "", THEIRS: ""})
    assert oidc.problem() == P5_FEDERATED


# ---- plain http ------------------------------------------------------------------------


def non_loopback_address():
    """This machine's own address that isn't loopback (no packet is sent to find it). Fails, rather than skips,
    where there is none: the tests that need it are the only proof of what happens off loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    assert not ipaddress.ip_address(address).is_loopback, address
    return address


@pytest.fixture
def far_idp():
    """A fake provider listening on every address, its addresses naming this machine's own that isn't loopback;
    trusting() sets it up for the platform's token."""
    provider = fake_idp.serve("0.0.0.0", public_host=non_loopback_address())
    provider.redirect_uri = CALLBACK
    yield provider
    provider.stop()


def oidc_lines(caplog, text=None):
    lines = [record.getMessage() for record in caplog.records if record.name == "ui.oidc"]
    return lines if text is None else lines.count(text)


def test_plain_http_needs_allow_http_and_is_warned(grid, db, far_idp, monkeypatch, tmp_path, caplog):
    """Without GRID_OIDC_ALLOW_HTTP the platform's token goes over plain http to a loopback host only; with it, to
    any host, and the log (once) and the Single sign-on tab say so."""
    caplog.set_level(logging.WARNING, logger="ui.oidc")
    fetch, fetched = oidc._fetch, []

    def recorded(url, **kwargs):
        fetched.append(url)
        return fetch(url, **kwargs)

    monkeypatch.setattr(oidc, "_fetch", recorded)
    # (a) An http issuer elsewhere, without the setting: the sentence every method meets, and nothing is fetched.
    provider = trusting(far_idp, monkeypatch, tmp_path)
    monkeypatch.setenv("GRID_OIDC_ISSUER", "http://idp.example.com")
    assert oidc.problem() == HTTPS_ISSUER and oidc.client_method() == "federated"
    assert signin_code(start(grid)) == "misconfigured" and fetched == []
    # (b) The provider at this machine's address that isn't loopback, over http.
    monkeypatch.setenv("GRID_OIDC_ISSUER", provider.base_url)
    assert not provider.base_url.startswith(("http://127.", "http://localhost"))
    assert oidc.problem() == HTTPS_ISSUER and oidc.plain_http_warning() is None
    assert signin_code(start(grid)) == "misconfigured" and provider.counts["token"] == 0 and fetched == []
    assert told(grid, headers=ADMIN_TOKEN)["warning"] is None
    monkeypatch.setenv("GRID_OIDC_ALLOW_HTTP", "1")
    for _ in range(2):
        assert signin_code(through(grid, provider)) is None
        for _ in range(5):
            assert grid.get("/api/session", headers=ADMIN_TOKEN).status_code == 200
    assert [a["raw"] for a in provider.assertions] == [provider.token] * 2 and provider.counts["token"] == 2
    assert oidc_lines(caplog, W1) == 1 and oidc_lines(caplog, GENERAL_HTTP) == 0
    body = told(grid, headers=ADMIN_TOKEN)
    assert (body["warning"], body["problem"]) == (W1, None)
    assert oidc_lines(caplog, W1) == 1


def test_an_http_token_endpoint_elsewhere_needs_allow_http_and_is_warned(grid, db, far_idp, monkeypatch, tmp_path,
                                                                         caplog):
    """(c, g) An issuer on loopback whose token endpoint is http elsewhere: without the setting the document is
    refused; with it the sign-in completes, and the warning is logged at the first start, before anyone is sent
    off, and shown once discovery is held."""
    caplog.set_level(logging.WARNING, logger="ui.oidc")
    provider = trusting(far_idp, monkeypatch, tmp_path)
    port = provider.server.server_port
    monkeypatch.setenv("GRID_OIDC_ISSUER", "http://127.0.0.1:{}".format(port))
    provider.origin = "http://127.0.0.1:{}".format(port)
    provider.token_endpoint = "http://{}:{}/token".format(non_loopback_address(), port)
    assert oidc.problem() is None
    assert signin_code(start(grid)) == "misconfigured" and provider.counts["token"] == 0
    with pytest.raises(oidc.Misconfigured) as wrong:
        oidc.check_client()
    assert str(wrong.value) == "the discovery document's token_endpoint isn't an https URL"
    monkeypatch.setenv("GRID_OIDC_ALLOW_HTTP", "1")
    oidc.reset_caches()
    assert oidc.problem() is None and oidc.plain_http_warning() is None  # nothing held yet, and nothing fetched
    assert oidc_lines(caplog, W1) == 0
    r = start(grid)  # the 303 is read, not followed
    assert r.status_code == 303 and r.headers["Location"].startswith(provider.base_url + "/authorize?")
    assert oidc_lines(caplog, W1) == 1 and provider.counts["token"] == 0
    assert signin_code(grid.get(local(provider.approve(r.headers["Location"])))) is None
    assert [a["raw"] for a in provider.assertions] == [provider.token]
    assert oidc.plain_http_warning() == W1 and told(grid, headers=ADMIN_TOKEN)["warning"] == W1
    assert oidc_lines(caplog, W1) == 1


def test_loopback_http_needs_no_setting_and_no_warning(grid, db, cluster_idp, caplog):
    """(d) A provider on loopback over http, no setting: signs in, and nothing is warned."""
    caplog.set_level(logging.WARNING, logger="ui.oidc")
    assert signin_code(through(grid, cluster_idp)) is None
    assert oidc.plain_http_warning() is None and told(grid, headers=ADMIN_TOKEN)["warning"] is None
    assert oidc_lines(caplog) == []


def test_a_secret_over_plain_http_gets_the_general_warning(grid, db, far_idp, monkeypatch, caplog):
    """(e) A client secret with GRID_OIDC_ALLOW_HTTP=1 and an http issuer elsewhere: only the general warning."""
    caplog.set_level(logging.WARNING, logger="ui.oidc")
    monkeypatch.setenv("GRID_OIDC_ISSUER", far_idp.base_url)
    monkeypatch.setenv("GRID_OIDC_ALLOW_HTTP", "1")
    assert signin_code(through(grid, far_idp)) is None and far_idp.auth_used == ["client_secret_basic"]
    assert oidc.plain_http_warning() is None and told(grid, headers=ADMIN_TOKEN)["warning"] is None
    assert oidc_lines(caplog) == [GENERAL_HTTP]


def test_a_held_http_token_endpoint_after_the_setting_is_removed(grid, db, cluster_idp, monkeypatch, caplog):
    """(f) X1: the document was held while GRID_OIDC_ALLOW_HTTP=1, and the setting is gone. With a client secret
    the same document is used as before."""
    provider = cluster_idp
    _plain_http(provider, monkeypatch)
    with pytest.raises(oidc.Misconfigured) as wrong:
        oidc.check_client()
    assert str(wrong.value) == PLAIN_HTTP
    with caplog.at_level(logging.WARNING, logger="ui.app"):
        r = start(grid)
    assert signin_code(r) == "misconfigured" and cookie_set(r, "grid-login") is None and count(db, "oidcLogins") == 0
    assert [record.getMessage() for record in caplog.records if record.name == "ui.app"] == [
        "couldn't start a sign-in: " + PLAIN_HTTP]
    with pytest.raises(oidc.Misconfigured) as wrong:
        oidc.redeem("a-code", "a-verifier")
    assert str(wrong.value) == PLAIN_HTTP and provider.counts["token"] == 0 and provider.assertions == []
    with monkeypatch.context() as secret:
        secret.setenv("GRID_OIDC_CLIENT_SECRET", fake_idp.CLIENT_SECRET)
        secret.delenv("GRID_OIDC_CLIENT_AUTH")
        secret.delenv(OURS)
        requests = token_requests(secret, answer={"id_token": "made-up"})
        oidc.check_client()
        assert oidc.redeem("a-code", "a-verifier") == "made-up"
        ((url, form, headers),) = requests
        assert url == provider.token_endpoint and "Authorization" in headers and "client_assertion" not in form
    # Loopback, by each of its names, needs no setting.
    for loopback in ("http://127.0.0.1:9/token", "http://localhost:9/token", "http://[::1]:9/token"):
        provider.token_endpoint = loopback
        oidc.reset_caches()
        oidc.check_client()
        requests = token_requests(monkeypatch, answer={"id_token": "made-up"})
        assert oidc.redeem("a-code", "a-verifier") == "made-up"
        url, form, headers = requests[-1]
        assert (url, form["client_assertion"]) == (loopback, provider.token) and "Authorization" not in headers


# The managed identity endpoint addresses that are refused, with GRID_OIDC_ALLOW_HTTP=1 as without ({port}: a
# recorder's, where the address is on loopback), and those that pass.
REFUSED_ENDPOINTS = ["http://8.8.8.8/msi/token", "http://100.64.0.1/msi/token", "http://[2001:db8::1]/msi/token",
                     "http://[::ffff:10.0.0.5]/msi/token", "http://0.0.0.0/msi/token", "http://example.com/msi/token",
                     "https://127.0.0.1:{port}/msi/token", "http://user:pw@127.0.0.1:{port}/msi/token",
                     "http://127.0.0.1.example.com/", "http://127.0.0.1:{port}/msi/token?object_id=x",
                     "http://127.0.0.1:{port}/msi/token?", "http://127.0.0.1:{port}/msi/token#x",
                     "http://127.0.0.1:{port}/msi/to ken"]
LOCAL_ENDPOINTS = ["http://localhost/msi/token", "http://127.0.0.1/msi/token", "http://[::1]/msi/token",
                   "http://169.254.129.2:8081/msi/token", "http://172.16.1.5:8081/msi/token", "http://10.0.0.5/msi/token",
                   "http://192.168.1.1/msi/token", "http://[fd00::1]/msi/token"]


@pytest.mark.parametrize("address", REFUSED_ENDPOINTS)
def test_allow_http_never_touches_the_identity_endpoint(monkeypatch, address):
    recorder = fake_idp.serve_recorder()
    try:
        configure(monkeypatch, GRID_OIDC_ISSUER=fake_idp.TENANT_ISSUER, GRID_OIDC_CLIENT_SECRET=None,
                  GRID_OIDC_CLIENT_AUTH="federated", GRID_OIDC_MANAGED_IDENTITY=fake_idp.CLIENT_ID_MI,
                  GRID_OIDC_ALLOW_HTTP="1", IDENTITY_ENDPOINT=address.format(port=recorder.server.server_port),
                  IDENTITY_HEADER="a-header-value")
        assert oidc._identity_endpoint()[0] is None
        assert oidc.problem().startswith("GRID_OIDC_MANAGED_IDENTITY can't be used: IDENTITY_ENDPOINT is ")
        assert oidc._managed_identity_token(oidc.settings())[0] is None and oidc._identity_memo is None
        assert recorder.count == 0
    finally:
        recorder.stop()


def proxied_through(recorder, monkeypatch, url):
    """Every proxy setting points at the recorder, and nothing is exempt from it: a plain request to url, as the
    environment would send it, does reach the recorder (so a test can't pass because the environment already
    bypasses it)."""
    for name in ("NO_PROXY", "no_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, recorder.url)
    # A fresh opener reads the environment now; urlopen's own would keep the first proxy it ever read.
    with pytest.raises(urllib.error.HTTPError) as answered:
        urllib.request.build_opener().open(url, timeout=5)
    answered.value.close()
    assert answered.value.code == 502 and recorder.count == 1


def test_plain_http_skips_any_proxy(grid, db, cluster_idp, monkeypatch, tmp_path):
    """No plain-http request to the provider goes through a proxy, whatever the method; https keeps the proxy, through
    which TLS runs end to end."""
    provider = cluster_idp
    recorder = fake_idp.serve_recorder()
    try:
        proxied_through(recorder, monkeypatch, provider.base_url + "/.well-known/openid-configuration")
        assert signin_code(through(grid, provider)) is None and provider.auth_used == ["federated"]
        with monkeypatch.context() as secret:
            secret.setenv("GRID_OIDC_CLIENT_SECRET", fake_idp.CLIENT_SECRET)
            secret.delenv("GRID_OIDC_CLIENT_AUTH")
            secret.delenv(OURS)
            oidc.reset_caches()
            assert signin_code(through(grid, provider)) is None
        assert provider.auth_used == ["federated", "client_secret_basic"] and recorder.count == 1
        # https keeps it: the proxy is asked (and, being a recorder, can't connect anyone).
        ca, certificate, key = fake_idp.write_server_certificate(tmp_path)
        secure = fake_idp.serve("127.0.0.1", tls=(certificate, key))
        try:
            secure.redirect_uri = CALLBACK
            trusting(secure, monkeypatch, tmp_path)
            monkeypatch.setenv("GRID_OIDC_ISSUER", secure.base_url)
            monkeypatch.setenv("GRID_OIDC_CA_FILE", ca)
            oidc.reset_caches()
            assert signin_code(start(grid)) == "unavailable"
            assert recorder.count == 2 and recorder.seen[-1][0].startswith("CONNECT 127.0.0.1:")
            assert secure.counts["discovery"] == 0
        finally:
            secure.stop()
    finally:
        recorder.stop()


def test_the_grid_ui_on_loopback_with_an_https_issuer(db, monkeypatch, tmp_path, caplog):
    """GRID_UI_PUBLIC_URL at http://127.0.0.1:<port>/ and the provider over https: no setting, no warning, with a
    token file and with a managed identity."""
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(grid_app, "PROXY_HOPS", 0)
    grid = grid_app.app.test_client()
    ca, certificate, key = fake_idp.write_server_certificate(tmp_path)
    provider, endpoint = fake_idp.serve("127.0.0.1", tls=(certificate, key)), fake_idp.serve_identity()
    try:
        public = "http://127.0.0.1:8400/"
        provider.redirect_uri = public + "auth/callback"
        configure(monkeypatch, GRID_OIDC_ISSUER=provider.base_url, GRID_UI_PUBLIC_URL=public, GRID_OIDC_CA_FILE=ca)
        trusting(provider, monkeypatch, tmp_path)
        assert provider.base_url.startswith("https://127.0.0.1:")
        assert oidc.problem() is None
        assert signin_code(through(grid, provider, base_url="http://127.0.0.1:8400")) is None
        assert told(grid, headers=ADMIN_TOKEN)["warning"] is None
        # A managed identity, the https fake standing in for Entra.
        monkeypatch.setitem(oidc.ENTRA_AUDIENCES, "127.0.0.1", AUDIENCE)
        monkeypatch.delenv(OURS)
        monkeypatch.setenv("GRID_OIDC_MANAGED_IDENTITY", fake_idp.CLIENT_ID_MI)
        monkeypatch.setenv("IDENTITY_ENDPOINT", endpoint.app_service_url)
        monkeypatch.setenv("IDENTITY_HEADER", endpoint.header)
        provider.trust(endpoint.tenant, subject=fake_idp.OBJECT_ID_MI)
        assert oidc.problem() is None
        assert signin_code(through(grid, provider, base_url="http://127.0.0.1:8400")) is None
        assert provider.assertions[-1]["raw"] == endpoint.issued[-1]
        assert told(grid, headers=ADMIN_TOKEN)["warning"] is None
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and r.name.startswith("ui.")] == []
    finally:
        provider.stop()
        endpoint.stop()


# ---- the token file ---------------------------------------------------------------------


def _missing(path, monkeypatch):
    path.unlink()


def _unreadable(path, monkeypatch):
    monkeypatch.setattr(oidc.os, "access", lambda *args, **kwargs: False)


def _directory(path, monkeypatch):
    path.unlink()
    path.mkdir()


def _unopenable(path, monkeypatch):
    def refused(*args, **kwargs):
        raise OSError("an-error-text-nobody-should-read")

    monkeypatch.setattr(oidc, "open", refused, raising=False)


def _written(content):
    def write(path, monkeypatch):
        path.write_bytes(content(path) if callable(content) else content)
    return write


def _made_by(claims):
    def write(path, monkeypatch):
        raw_token(path, claims)
    return write


def _expired(path, monkeypatch):
    write_token(path, fake_idp.FakeCluster(), exp=int(time.time()) - 61)


FILE_PROBLEMS = [
    ("missing", _missing, "there is no file {p}"),
    ("unreadable", _unreadable, "the Grid UI's user may not read {p}"),
    ("a directory", _directory, "there is no file {p}"),
    ("not to be opened", _unopenable, "{p} can't be opened"),
    ("empty", _written(b""), "{p} is empty"),
    ("blank", _written(b" \r\n\t\n"), "{p} is empty"),
    ("16385 bytes", _written(b"a" * 8000 + b"." + b"b" * 8000 + b"." + b"c" * 383), "{p} holds more than 16384 bytes"),
    ("not ASCII", _written(lambda path: path.read_bytes() + "é".encode()), "{p} doesn't hold a token (three base64url parts)"),
    ("two parts", _written(lambda path: path.read_bytes().rsplit(b".", 1)[0]), "{p} doesn't hold a token (three base64url parts)"),
    ("four parts", _written(lambda path: path.read_bytes() + b".again"), "{p} doesn't hold a token (three base64url parts)"),
    ("two tokens", _written(lambda path: path.read_bytes() + b"\n" + path.read_bytes()), "{p} doesn't hold a token (three base64url parts)"),
    ("a payload that is not JSON", _made_by("{not json"), "{p} doesn't hold a token (three base64url parts)"),
    ("a payload that is a list", _made_by([1, 2]), "{p} doesn't hold a token (three base64url parts)"),
    ("a payload that is a string", _made_by('"exp"'), "{p} doesn't hold a token (three base64url parts)"),
    ("expired", _expired, "the token in {p} has expired; the platform should have replaced it"),
]


@pytest.mark.parametrize("name", [OURS, THEIRS])
@pytest.mark.parametrize("case, spoil, reason", FILE_PROBLEMS, ids=[row[0] for row in FILE_PROBLEMS])
def test_what_is_wrong_with_the_token_file(cluster_idp, monkeypatch, name, case, spoil, reason):
    """Each is a problem by its own sentence, and nothing is sent: not the file's token, and nothing in its place."""
    provider, path = cluster_idp, cluster_idp.token_file
    platform_names(monkeypatch, path, name)
    assert oidc.problem() is None
    oidc.discovery()
    spoil(path, monkeypatch)
    sentence = "{} can't be used: {}.".format(name, reason.format(p=path))
    assert oidc.problem() == sentence
    assert oidc.client_method() == "federated"  # the settings still say so: no other method takes over
    with pytest.raises(oidc.Misconfigured) as wrong:
        oidc.redeem("a-code", "a-verifier")
    assert str(wrong.value) == sentence and wrong.value.__cause__ is None and wrong.value.__context__ is None
    assert provider.counts["token"] == 0 and provider.auth_used == []


def test_a_token_file_of_the_largest_size_is_read(cluster_idp):
    token = raw_token(cluster_idp.token_file, {"sub": "s" * 11000})
    padded = token.encode() + b"\n" * (oidc.TOKEN_FILE_LIMIT - len(token))
    cluster_idp.token_file.write_bytes(padded)
    assert len(padded) == 16384 and oidc.problem() is None
    assert oidc._federated_token(oidc.settings())[0] == token


TOKEN_CLAIMS = '{{"iss":"https://oidc.cluster.example/a/","sub":"system:serviceaccount:grid:grid-ui","aud":["a"]{}}}'


@pytest.mark.parametrize("exp", ["1" + "0" * 400, "1e400", "NaN", "Infinity", "-Infinity", "-1", "0", "0.0",
                                 "100000000000", "1e11", '"1790000000"', "true", "false", "null", "[1790000000]",
                                 '{"at":1790000000}', None])
def test_an_exp_that_is_no_date(cluster_idp, exp):
    """It counts as stating none: the token is sent, and the provider judges it."""
    provider = cluster_idp
    token = raw_token(provider.token_file, TOKEN_CLAIMS.format("" if exp is None else ',"exp":' + exp))
    claims = oidc._token_claims(token)
    assert claims["sub"] == SERVICE_ACCOUNT and oidc._expiry(claims) is None
    assert oidc.problem() is None
    facts = oidc.federated_token_facts()
    assert (facts["decoded"], facts["expiresAt"], facts["expired"]) == (True, None, False)
    with pytest.raises(oidc.HttpRefused):  # the fake trusts no such issuer
        sign_in(provider)
    assert [a["raw"] for a in provider.assertions] == [token] and provider.auth_used == ["federated"]


def test_an_exp_too_long_to_read_is_no_token(cluster_idp):
    """Python refuses to read an integer of 5000 digits: the file then holds no token, and nothing raises."""
    provider = cluster_idp
    raw_token(provider.token_file, TOKEN_CLAIMS.format(',"exp":' + "9" * 5000))
    sentence = "{} can't be used: {} doesn't hold a token (three base64url parts).".format(OURS, provider.token_file)
    assert oidc.problem() == sentence
    facts = oidc.federated_token_facts()
    assert (facts["decoded"], facts["issuer"], facts["expiresAt"], facts["expired"]) == (False, None, None, False)
    with pytest.raises(oidc.Misconfigured):
        sign_in(provider)
    assert provider.counts["token"] == 0


def test_a_payload_nested_deep_is_no_token(cluster_idp):
    raw_token(cluster_idp.token_file, "[" * 3000 + "]" * 3000)
    assert oidc.problem().endswith("doesn't hold a token (three base64url parts).")
    assert oidc.federated_token_facts()["decoded"] is False
    raw_token(cluster_idp.token_file, '{"exp":' + "[" * 200 + "]" * 200 + "}")
    assert oidc.problem() is None and oidc.federated_token_facts()["expiresAt"] is None


def test_a_token_within_the_clock_skew_is_still_sent(cluster_idp, monkeypatch):
    provider = cluster_idp
    token = write_token(provider.token_file, provider.cluster, exp=int(time.time()) - 30)
    provider.clock_offset_s = -60  # a provider whose clock is behind takes it still
    assert oidc.problem() is None and oidc.federated_token_facts()["expired"] is False
    monkeypatch.setenv("GRID_OIDC_CLOCK_SKEW_S", "120")  # for the ID token of a provider that far behind
    assert oidc.verify_id_token(*sign_in(provider))["sub"] == SUBJECT
    assert [a["raw"] for a in provider.assertions] == [token]


@pytest.mark.parametrize("skew, ago", [(None, 61), ("0", 1), ("300", 301)])
def test_an_expired_token_is_never_sent(grid, db, cluster_idp, monkeypatch, skew, ago):
    provider = cluster_idp
    if skew is not None:
        monkeypatch.setenv("GRID_OIDC_CLOCK_SKEW_S", skew)
    r = start(grid)
    callback = provider.approve(r.headers["Location"])
    write_token(provider.token_file, provider.cluster, exp=int(time.time()) - ago)
    sentence = "{} can't be used: the token in {} has expired; the platform should have replaced it.".format(
        OURS, provider.token_file)
    assert oidc.problem() == sentence and oidc.federated_token_facts()["expired"] is True
    assert signin_code(grid.get(local(callback))) == "misconfigured"
    assert signin_code(start(grid)) == "misconfigured"
    with pytest.raises(oidc.Misconfigured):
        sign_in(provider)
    assert provider.counts["token"] == 0 and provider.assertions == [] and count(db, "userSessions") == 0


# ---- the platform's token in a sign-in -----------------------------------------------------


def test_a_sign_in_with_the_platforms_token(grid, db, cluster_idp, monkeypatch):
    provider = cluster_idp
    requests = token_requests(monkeypatch)
    assert oidc.problem() is None and oidc.client_method() == "federated"
    r = through(grid, provider)
    assert signin_code(r) is None and cookie_set(r, "grid-session") and count(db, "userSessions") == 1
    assert run(db, "SELECT subject FROM users") == [(SUBJECT,)]
    assert provider.auth_used == ["federated"] and provider.counts["token"] == 1
    ((url, form, headers),) = requests
    assert url == provider.base_url + "/token"
    assert sorted(form) == ["client_assertion", "client_assertion_type", "client_id", "code", "code_verifier",
                            "grant_type", "redirect_uri"]
    assert (form["client_id"], form["client_assertion_type"], form["client_assertion"]) == (
        fake_idp.CLIENT_ID, "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        provider.token_file.read_text())
    assert headers == {"Content-Type": "application/x-www-form-urlencoded"}
    (assertion,) = provider.assertions
    assert assertion["raw"] == provider.token and assertion["claims"]["sub"] == SERVICE_ACCOUNT


def test_the_token_is_read_again_for_every_redemption(cluster_idp):
    provider = cluster_idp
    first = provider.token
    assert oidc.verify_id_token(*sign_in(provider))["sub"] == SUBJECT
    second = write_token(provider.token_file, provider.cluster, lifetime_s=7200)
    assert oidc.verify_id_token(*sign_in(provider))["sub"] == SUBJECT
    assert oidc.verify_id_token(*sign_in(provider))["sub"] == SUBJECT  # one token serves until it expires
    # As a file someone wrote by hand ends: the token is sent without the line break.
    third = provider.cluster.token(lifetime_s=600)
    provider.token_file.write_bytes(third.encode() + b"\r\n")
    assert oidc.verify_id_token(*sign_in(provider))["sub"] == SUBJECT
    assert len({first, second, third}) == 3
    assert [a["raw"] for a in provider.assertions] == [first, second, second, third]


def _held(value, needles, seen):
    """Whether any of needles is in value: a str, bytes, or whatever a container or an object holds."""
    if id(value) in seen:
        return False
    seen.add(id(value))
    if isinstance(value, bytes):
        value = value.decode("latin-1")
    if isinstance(value, str):
        return any(needle in value for needle in needles)
    if isinstance(value, dict):
        return any(_held(part, needles, seen) for pair in value.items() for part in pair)
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_held(part, needles, seen) for part in value)
    if isinstance(value, (types.ModuleType, type)):
        return False
    held = [getattr(value, name, None) for name in ("__dict__", "__defaults__", "__kwdefaults__")]
    cells = [cell.cell_contents for cell in getattr(value, "__closure__", None) or () if cell is not None]
    return any(_held(part, needles, seen) for part in held + cells if part is not None)


def test_nothing_keeps_the_token(grid, db, cluster_idp):
    provider = cluster_idp
    assert signin_code(through(grid, provider)) is None
    assert oidc.problem() is None and oidc.federated_token_facts()["decoded"] and oidc.provider_problem() is None
    assert grid.get("/api/admin/sign-in", headers=ADMIN_TOKEN).status_code == 200
    needles = [provider.token] + provider.token.split(".")
    assert _held({"kept": [provider.token]}, needles, set())  # the search finds one that is kept
    holders = [name for name, value in vars(oidc).items() if _held(value, needles, set())]
    assert holders == []
    assert oidc._discovered is not None  # what is kept, is kept: the search ran over a filled cache


def test_a_token_file_that_goes_bad_after_the_start(grid, db, cluster_idp, caplog):
    provider = cluster_idp
    r = start(grid)
    callback = provider.approve(r.headers["Location"])
    provider.token_file.unlink()
    with caplog.at_level(logging.WARNING):
        r = grid.get(local(callback))
    sentence = "{} can't be used: there is no file {}.".format(OURS, provider.token_file)
    assert signin_code(r) == "misconfigured" and cookie_set(r, "grid-session") is None
    assert count(db, "userSessions") == 0 and provider.counts["token"] == 0 and provider.auth_used == []
    assert [(record.name, record.getMessage()) for record in caplog.records if record.levelno >= logging.WARNING] == [
        ("ui.app", "couldn't redeem a sign-in's code: " + sentence)]
    assert session_problems(grid) == [None, sentence]
    # The platform writes it again: the next sign-in works.
    write_token(provider.token_file, provider.cluster)
    assert signin_code(through(grid, provider)) is None and provider.auth_used == ["federated"]


# ---- fail closed, and the admin is told ----------------------------------------------------


def sentence_of(mismatch):
    return mismatch[:1].upper() + mismatch[1:] + "."


def test_a_provider_that_lists_no_assertions_stops_the_sign_in_before_it_starts(grid, db, cluster_idp, caplog):
    provider = cluster_idp
    provider.auth_methods = ["client_secret_basic", "client_secret_post"]
    with caplog.at_level(logging.WARNING):
        r = start(grid)
    assert signin_code(r) == "misconfigured" and cookie_set(r, "grid-login") is None
    assert count(db, "oidcLogins") == 0 and provider.counts["authorize"] == 0 and provider.counts["token"] == 0
    assert [(record.name, record.getMessage()) for record in caplog.records if record.levelno >= logging.WARNING] == [
        ("ui.app", "couldn't start a sign-in: " + NO_ASSERTIONS)]
    body = grid.get("/api/admin/sign-in", headers=ADMIN_TOKEN).get_json()
    assert body["problem"] == ("The identity provider doesn't take client assertions: private_key_jwt isn't among the "
                               "token_endpoint_auth_methods_supported its discovery document lists.")
    assert (body["clientMethod"], body["clientToken"]["decoded"]) == ("federated", True)
    assert session_problems(grid) == [None, None]  # the page's own question doesn't wait for the provider
    with pytest.raises(oidc.Misconfigured) as wrong:
        oidc.redeem("a-code", "a-verifier")
    assert str(wrong.value) == NO_ASSERTIONS and provider.counts["token"] == 0 and provider.auth_used == []


@pytest.mark.parametrize("listed", [None, []])
def test_a_provider_that_lists_nothing_is_sent_the_token(cluster_idp, listed):
    """Discovery need not list the methods. The fake, like any provider, then takes what it takes: here it refuses
    the assertion, having received it."""
    provider = cluster_idp
    provider.auth_methods = listed
    oidc.check_client()
    assert oidc.provider_problem() is None
    with pytest.raises(oidc.HttpRefused) as refused:
        sign_in(provider)
    assert (refused.value.status, refused.value.error, refused.value.codes) == (401, "invalid_client", ())
    assert [a["raw"] for a in provider.assertions] == [provider.token] and provider.auth_used == ["federated"]


def test_the_provider_problem(grid, db, cluster_idp, monkeypatch):
    provider = cluster_idp
    clock = stopped_clock(monkeypatch)
    # With a client secret the provider isn't asked.
    with monkeypatch.context() as secret:
        secret.setenv("GRID_OIDC_CLIENT_SECRET", fake_idp.CLIENT_SECRET)
        secret.delenv("GRID_OIDC_CLIENT_AUTH")
        secret.delenv(OURS)
        provider.auth_methods = ["client_secret_post"]
        assert oidc.provider_problem() is None and provider.counts["discovery"] == 0
    # Nor while the settings have a problem of their own.
    provider.token_file.unlink()
    assert oidc.problem() and oidc.provider_problem() is None and provider.counts["discovery"] == 0
    write_token(provider.token_file, provider.cluster)
    # A provider that can't be reached is no problem of the settings.
    provider.fault = "discovery_down"
    assert oidc.provider_problem() is None and provider.counts["discovery"] == 1
    assert session_problems(grid) == [None, None]
    provider.fault = None
    # Each mismatch, by its sentence; asked again within the hour, nothing is fetched.
    oidc.reset_caches()
    asked = provider.counts["discovery"]
    for _ in range(3):
        assert oidc.provider_problem() == sentence_of(NO_ASSERTIONS)
        clock[0] += oidc.CACHE_S / 4
    assert provider.counts["discovery"] == asked + 1
    assert session_problems(grid) == [None, None]
    assert grid.get("/api/admin/sign-in", headers=ADMIN_TOKEN).get_json()["problem"] == sentence_of(NO_ASSERTIONS)
    provider.auth_methods = list(ALL_METHODS)
    provider.token_endpoint = "http://idp.example.com/token"
    monkeypatch.setenv("GRID_OIDC_ALLOW_HTTP", "1")
    oidc.reset_caches()
    assert oidc.provider_problem() is None and oidc.plain_http_warning() == W1  # allowed, and warned
    monkeypatch.delenv("GRID_OIDC_ALLOW_HTTP")
    assert oidc.provider_problem() == sentence_of(PLAIN_HTTP)  # the document held from before the change: X1
    oidc.reset_caches()
    assert oidc.provider_problem() == "The discovery document's token_endpoint isn't an https URL."
    provider.token_endpoint = None
    provider.fault = "discovery_issuer_mismatch"
    oidc.reset_caches()
    assert oidc.provider_problem() == (
        "The discovery document's issuer is {!r}, but GRID_OIDC_ISSUER is {!r}: they must be equal, trailing / and all.".format(
            provider.origin + "/other", provider.base_url))
    assert session_problems(grid) == [None, None]
    provider.fault = None
    provider.algs = ["HS256"]
    oidc.reset_caches()
    assert oidc.provider_problem() == "The identity provider signs ID tokens with none of {}.".format(
        ", ".join(oidc.ALLOWED_ALGS))
    # Nothing wrong: nothing said.
    provider.algs = ["RS256"]
    oidc.reset_caches()
    asked = provider.counts["discovery"]
    assert oidc.provider_problem() is None and oidc.provider_problem() is None
    assert provider.counts["discovery"] == asked + 1 and provider.counts["token"] == 0 and provider.counts["jwks"] == 0
    assert grid.get("/api/admin/sign-in", headers=ADMIN_TOKEN).get_json()["problem"] is None


def _no_assertions(provider, monkeypatch):
    provider.auth_methods = ["client_secret_basic", "client_secret_post"]
    oidc.reset_caches()


def _lists_only_secrets_and_takes_them(provider, monkeypatch):
    provider.auth_methods = ["client_secret_basic"]
    provider.client_secret = ""  # a provider that would take an empty secret, were one sent
    oidc.reset_caches()


def _plain_http(provider, monkeypatch):
    """A discovery document with a plain http token endpoint elsewhere, held from while GRID_OIDC_ALLOW_HTTP=1 was
    set, and the setting removed since (X1)."""
    monkeypatch.setenv("GRID_OIDC_ALLOW_HTTP", "1")
    provider.token_endpoint = "http://idp.example.com/token"
    oidc.reset_caches()
    oidc.discovery()
    monkeypatch.delenv("GRID_OIDC_ALLOW_HTTP")


def _token(**changes):
    def write(provider, monkeypatch):
        write_token(provider.token_file, provider.cluster, **changes)
    return write


def _untrusted(provider, monkeypatch):
    del provider.federated[:]


def _another_key(provider, monkeypatch):
    write_token(provider.token_file, fake_idp.FakeCluster(provider.cluster.issuer))


def _fault(fault):
    def spoil(provider, monkeypatch):
        provider.fault = fault
    return spoil


NOT_SENT = [
    ("the file is gone", lambda provider, monkeypatch: provider.token_file.unlink()),
    ("the file is empty", lambda provider, monkeypatch: provider.token_file.write_bytes(b"")),
    ("the file holds no token", lambda provider, monkeypatch: provider.token_file.write_bytes(b"s3cret-client")),
    ("the token expired", _token(exp=int(time.time()) - 3600)),
    ("no assertions listed", _no_assertions),
    ("only secrets listed, an empty one taken", _lists_only_secrets_and_takes_them),
    ("a plain http token endpoint", _plain_http),
]
REFUSALS = [
    ("another subject", _token(subject="system:serviceaccount:grid:other"), "misconfigured"),
    ("another audience", _token(audience="api://AKSIdentityBinding"), "misconfigured"),
    ("no federated credential", _untrusted, "misconfigured"),
    ("another cluster's key", _another_key, "misconfigured"),
    ("Entra's 70021", _fault("token_401_entra_70021"), "misconfigured"),
    ("a plain invalid_client", _fault("token_401_invalid_client"), "misconfigured"),
    ("invalid_grant with a number", _fault("token_400_grant_with_number"), "failed"),
    ("a server error", _fault("token_500"), "unavailable"),
]


@pytest.mark.parametrize("case, spoil, code", [row + ("misconfigured",) for row in NOT_SENT] + REFUSALS,
                         ids=[row[0] for row in NOT_SENT + REFUSALS])
def test_no_other_method_is_tried(grid, db, cluster_idp, monkeypatch, case, spoil, code):
    """Whatever keeps the platform's token from working, the sign-in ends there: no secret is tried in its place,
    in that request or the next, and nobody is signed in."""
    provider = cluster_idp
    requests = token_requests(monkeypatch)
    r = start(grid)
    callback = provider.approve(r.headers["Location"])
    spoil(provider, monkeypatch)
    r = grid.get(local(callback))
    assert signin_code(r) == code and cookie_set(r, "grid-session") is None
    sent = case in [row[0] for row in REFUSALS]
    r = start(grid)
    if sent:  # the provider refuses; the settings and the file are as good as before
        r = grid.get(local(provider.approve(r.headers["Location"])))
        assert cookie_set(r, "grid-session") is None
    assert signin_code(r) == code
    assert provider.auth_used == (["federated"] * 2 if sent and "fault" not in spoil.__qualname__ else [])
    assert provider.counts["token"] == (2 if sent else 0) and len(requests) == (2 if sent else 0)
    for _, form, headers in requests:
        assert "Authorization" not in headers and "client_secret" not in form
        assert form["client_assertion"] == provider.token_file.read_text()
    assert count(db, "userSessions") == 0 and count(db, "users") == 0


def test_check_client(cluster_idp, monkeypatch):
    provider = cluster_idp
    oidc.check_client()
    oidc.check_client()
    assert (provider.counts["discovery"], provider.counts["jwks"], provider.counts["token"]) == (1, 0, 0)
    for change, says in ((_no_assertions, NO_ASSERTIONS), (_plain_http, PLAIN_HTTP)):
        change(provider, monkeypatch)
        with pytest.raises(oidc.Misconfigured) as wrong:
            oidc.check_client()
        assert str(wrong.value) == says
    assert (provider.counts["jwks"], provider.counts["token"]) == (0, 0)
    provider.token_endpoint = None
    # A client secret is never judged against what discovery lists, as before.
    monkeypatch.setenv("GRID_OIDC_CLIENT_SECRET", fake_idp.CLIENT_SECRET)
    monkeypatch.delenv(OURS)
    for setting in ("auto", "basic", "post"):
        monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", setting)
        provider.auth_methods = ["private_key_jwt"]
        oidc.reset_caches()
        oidc.check_client()
    # Settings that don't say how: their own sentence, and what discovery() raises comes through.
    monkeypatch.delenv("GRID_OIDC_CLIENT_SECRET")
    monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", "auto")
    with pytest.raises(oidc.Misconfigured) as wrong:
        oidc.check_client()
    assert str(wrong.value) == P3
    provider.fault = "discovery_down"
    oidc.reset_caches()
    with pytest.raises(oidc.HttpRefused):
        oidc.check_client()
    provider.fault = "discovery_issuer_mismatch"
    with pytest.raises(oidc.Misconfigured):
        oidc.check_client()
    assert (provider.counts["jwks"], provider.counts["token"]) == (0, 0)


# ---- refusals ------------------------------------------------------------------------------


class Answer(io.BytesIO):
    """An error answer's body, as urllib hands it over."""

    def __init__(self, body):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())


ENTRA = {"error": "invalid_client",
         "error_description": "AADSTS70021: description-marker. Trace ID: 00000000-0000-0000-0000-000000000001",
         "error_codes": [70021], "timestamp": "2026-11-09 12:00:00Z",
         "trace_id": "00000000-0000-0000-0000-000000000001", "correlation_id": "00000000-0000-0000-0000-000000000002",
         "error_uri": "https://login.microsoftonline.com/error?code=70021"}


@pytest.mark.parametrize("body, kept", [
    (ENTRA, ("invalid_client", (70021,))),
    ({"error": "invalid_grant"}, ("invalid_grant", ())),
    ({"error": "invalid_client", "error_codes": ["70021", 700213, "description-marker"]}, ("invalid_client", (700213,))),
    ({"error": "invalid_client", "error_codes": [True, False, 5]}, ("invalid_client", (5,))),
    ({"error": "invalid_client", "error_codes": [-70021, 0, 10 ** 9, 10 ** 30, 999999999]}, ("invalid_client", (999999999,))),
    ({"error": "invalid_client", "error_codes": [70021.0, 1e3, None, [70021], {"code": 70021}]}, ("invalid_client", ())),
    ({"error": "invalid_client", "error_codes": [1, 2, 3, 4, 5, 6]}, ("invalid_client", (1, 2, 3, 4))),
    ({"error": "invalid_client", "error_codes": 70021}, ("invalid_client", ())),
    ({"error": "invalid_client", "error_codes": {"0": 70021}}, ("invalid_client", ())),
    ({"error": "invalid_client", "error_codes": "70021"}, ("invalid_client", ())),
    ({"error": ["invalid_client"], "error_codes": [70021]}, (None, (70021,))),
    ({"error": "description-marker <b>", "error_codes": [70021]}, (None, (70021,))),
    ({"error": "e" * 65}, (None, ())),
    ({"error_codes": [700016]}, (None, (700016,))),
    (dict(ENTRA, error_description="description-marker " * 1000), (None, ())),  # 20 kB: more than is read
    ([ENTRA], (None, ())),
    (b"description-marker, and not JSON", (None, ())),
    (b"", (None, ())),
    (b"[" * 5000, (None, ())),
])
def test_what_is_kept_of_an_error_answer(body, kept):
    """The OAuth error code and at most four of the provider's error numbers: never a word of its own."""
    answer = Answer(body)
    assert oidc._error_codes(answer) == kept and answer.closed
    refused = oidc.HttpRefused(401, *kept)
    assert (refused.status, refused.error, refused.codes) == (401,) + kept
    assert "description-marker" not in str(refused) and "0000" not in str(refused)


def test_how_a_refusal_reads():
    assert str(oidc.HttpRefused(401, "invalid_client", (70021,))) == "HTTP 401 invalid_client (error codes 70021)"
    assert str(oidc.HttpRefused(400, "invalid_grant", [700024, 70021])) == "HTTP 400 invalid_grant (error codes 700024, 70021)"
    assert str(oidc.HttpRefused(401, "invalid_client")) == "HTTP 401 invalid_client"
    assert str(oidc.HttpRefused(500)) == "HTTP 500" and oidc.HttpRefused(500).codes == ()
    assert str(oidc.HttpRefused(400, None, (7,))) == "HTTP 400 (error codes 7)"


def test_the_hint_by_number_then_by_method():
    methods = (None,) + oidc.CLIENT_METHODS + ("managed-identity",)
    assert sorted(oidc._ENTRA_HINTS) == [70021, 70025, 700016, 700024, 700211, 700212, 700213, 700238]
    for number, hint in oidc._ENTRA_HINTS.items():
        for method in methods:
            assert oidc.refusal_hint((number,), method) == "AADSTS{}: {}".format(number, hint)
            assert oidc.refusal_hint((9, number, 700016), method).startswith("AADSTS{}: ".format(number))
        assert hint.endswith(".") and "\n" not in hint
    assert oidc.refusal_hint((700213,)) == SUBJECT_HINT and oidc.refusal_hint([700024], "secret") == TIME_HINT
    assert oidc.refusal_hint((700212, 700213)).startswith("AADSTS700212: ")  # the first known one
    # A number that isn't known, or none: by the method, when the caller names one.
    for codes in ((), (7000215,), (50126, 1)):
        assert oidc.refusal_hint(codes, "federated") == FEDERATED_HINT
        # A managed identity's token states its issuer and audience in its own way: never told to copy them.
        assert oidc.refusal_hint(codes, "managed-identity") == MANAGED_IDENTITY_HINT
        assert "token's issuer" not in MANAGED_IDENTITY_HINT and "exactly" not in MANAGED_IDENTITY_HINT
        for method in ("secret", "basic", "post"):
            assert oidc.refusal_hint(codes, method) == SECRET_HINT
        assert oidc.refusal_hint(codes) == oidc.refusal_hint(codes, None) == ""
    assert oidc.refusal_hint((), "private_key_jwt") == ""


def _ask(provider, token, headers=None, **form):
    """The fake's answer to a token request with that client assertion: (status, error, error_codes, the body)."""
    sent = {"grant_type": "authorization_code", "code": "no-such-code", "redirect_uri": CALLBACK,
            "code_verifier": "v", "client_id": provider.client_id,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": token}
    sent.update(form)
    r = provider.app.test_client().post("/token", data={k: v for k, v in sent.items() if v is not None},
                                        headers=headers or {})
    body = r.get_json()
    return r.status_code, body["error"], body.get("error_codes"), body


TAKEN = (400, "invalid_grant", None)  # the client was taken; the made-up code is what is refused


def _refused(number=None):
    return 401, "invalid_client", None if number is None else [number]


def _signed_by(key, claims, alg="RS256"):
    import jwt
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": "cluster-1"})


def test_how_the_fake_refuses(idp):
    """The fake verifies a platform token as Entra verifies one against a federated identity credential, each check
    in its place: so no test here passes by sending it any string."""
    provider, cluster, other, stranger = idp, fake_idp.FakeCluster(), fake_idp.FakeCluster(), fake_idp.FakeCluster(
        "https://oidc.elsewhere.example/9/")
    impostor = fake_idp.FakeCluster(cluster.issuer)  # states the trusted issuer, signs with a key of its own
    provider.auth_methods = list(ALL_METHODS)
    provider.trust(cluster)
    good, now = cluster.token(), int(time.time())
    basic = {"Authorization": "Basic " + base64.b64encode(b"grid-ui:s3cret-client").decode()}
    wrong_sub, wrong_aud = "system:serviceaccount:grid:other", "api://AKSIdentityBinding"

    def ask(token=good, **kwargs):
        return _ask(provider, token, **kwargs)[:3]

    assert ask() == TAKEN and ask() == TAKEN  # a token is good until it expires
    assert ask(cluster.token(aud=AUDIENCE)) == TAKEN and ask(cluster.token(aud=["a", AUDIENCE])) == TAKEN
    # 1: the request's shape.
    assert ask(client_assertion_type="urn:ietf:params:oauth:client-assertion-type:saml2-bearer") == _refused()
    assert ask(client_assertion_type=None) == _refused()
    assert ask(client_secret=fake_idp.CLIENT_SECRET) == _refused()
    assert ask(headers=basic) == _refused()
    for not_a_token in ("", "an-arbitrary-string", good.rsplit(".", 1)[0], good + ".more", "a.b.c",
                        fake_idp._segment([1]) + "." + good.split(".", 1)[1],
                        good.split(".")[0] + "." + fake_idp._segment("claims") + "." + good.split(".")[2]):
        assert ask(not_a_token) == _refused()
    provider.auth_methods = ["client_secret_basic", "client_secret_post"]
    assert ask() == _refused()
    provider.auth_methods = None
    assert ask() == _refused()
    provider.auth_methods = list(ALL_METHODS)
    # 2: the client.
    assert ask(client_id="someone-else") == _refused(700016)
    assert ask(client_id=None) == _refused(700016)
    # 3: any federated credential at all.
    del provider.federated[:]
    assert ask() == _refused(70025)
    provider.trust(cluster)
    # 4: the issuer.
    assert ask(stranger.token()) == _refused(700211)
    assert ask(cluster.token(iss=cluster.issuer.rstrip("/"))) == _refused(700211)  # exactly, trailing / and all
    assert ask(cluster.token(iss=None)) == _refused(700211)
    # 5: the signature.
    assert ask(impostor.token()) == _refused()
    assert ask(good.rsplit(".", 1)[0] + "." + fake_idp._b64(b"not a signature")) == _refused()
    header, payload, signature = good.split(".")
    changed = dict(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))), exp=now + 10 ** 6)
    assert ask(header + "." + fake_idp._segment(changed) + "." + signature) == _refused()
    assert ask(fake_idp._segment({"alg": "none"}) + "." + payload + ".AAAA") == _refused()
    hs256 = _signed_by("k" * 32, {"iss": cluster.issuer, "sub": SERVICE_ACCOUNT, "aud": [AUDIENCE], "exp": now + 600},
                       "HS256")
    assert ask(hs256) == _refused()
    # 6: the expiry.
    assert ask(cluster.token(exp=now - 1)) == _refused(700024)
    assert ask(cluster.token(exp=None)) == _refused(700024)
    assert ask(cluster.token(exp=str(now + 600))) == _refused(700024)
    provider.clock_offset_s = 7200
    assert ask() == _refused(700024)
    provider.clock_offset_s = 0
    # 7: the subject.
    assert ask(cluster.token(subject=wrong_sub)) == _refused(700213)
    assert ask(cluster.token(subject=SERVICE_ACCOUNT.upper())) == _refused(700213)
    assert ask(cluster.token(sub=None)) == _refused(700213)
    # 8: the audience.
    assert ask(cluster.token(audience=wrong_aud)) == _refused(700212)
    assert ask(cluster.token(aud=None)) == _refused(700212)
    assert ask(cluster.token(aud=[AUDIENCE + "/"])) == _refused(700212)
    # The order, by pairs of faults.
    assert ask(stranger.token(subject=wrong_sub)) == _refused(700211)
    assert ask(cluster.token(subject=wrong_sub, audience=wrong_aud)) == _refused(700213)
    assert ask(cluster.token(subject=wrong_sub, exp=now - 1)) == _refused(700024)
    assert ask(impostor.token(subject=wrong_sub, exp=now - 1)) == _refused()
    assert ask(stranger.token(exp=now - 1)) == _refused(700211)
    assert ask(stranger.token(subject=wrong_sub, audience=wrong_aud, exp=now - 1), client_id="someone-else") == _refused(700016)
    del provider.federated[:]
    assert ask(stranger.token(), client_id="someone-else") == _refused(700016)
    assert ask(stranger.token(), client_secret="x") == _refused()
    # The audience is the one of a credential whose subject matched; a second cluster has its own key and credentials.
    provider.trust(cluster)
    provider.trust(cluster, subject=wrong_sub, audience=wrong_aud)
    provider.trust(other, subject="system:serviceaccount:ci:runner")
    assert ask(cluster.token(subject=wrong_sub, audience=wrong_aud)) == TAKEN
    assert ask(cluster.token(subject=wrong_sub)) == _refused(700212)
    assert ask(cluster.token(audience=wrong_aud)) == _refused(700212)
    assert ask(other.token(subject="system:serviceaccount:ci:runner")) == TAKEN
    assert ask(other.token()) == _refused(700213)
    assert ask(fake_idp.FakeCluster(other.issuer).token(subject="system:serviceaccount:ci:runner")) == _refused()
    # A refusal as Entra writes one, and what the fake notes of each assertion.
    body = _ask(provider, cluster.token(subject="system:serviceaccount:grid:nobody"))[3]
    assert body == {"error": "invalid_client", "error_codes": [700213], "timestamp": "2026-11-09 12:00:00Z",
                    "error_description": "AADSTS700213: description-marker 'system:serviceaccount:grid:nobody'. "
                                         "Trace ID: 00000000-0000-0000-0000-000000000001",
                    "trace_id": "00000000-0000-0000-0000-000000000001",
                    "correlation_id": "00000000-0000-0000-0000-000000000002"}
    body = _ask(provider, impostor.token())[3]
    assert "error_codes" not in body and "description-marker" in body["error_description"]
    assert set(provider.auth_used) == {"federated"}
    assert provider.assertions[0] == {"raw": good, "header": {"alg": "RS256", "kid": "cluster-1", "typ": "JWT"},
                                      "claims": provider.assertions[0]["claims"]}
    assert provider.assertions[0]["claims"]["iss"] == cluster.issuer
    for fault, answer in (("token_401_entra_70021", (401, "invalid_client", [70021])),
                          ("token_400_grant_with_number", (400, "invalid_grant", [700024]))):
        provider.fault = fault
        status, error, codes, body = _ask(provider, good)
        assert (status, error, codes) == answer and "description-marker" in body["error_description"]
        assert body["trace_id"] == fake_idp.TRACE_ID and body["correlation_id"] == fake_idp.CORRELATION_ID


def app_warnings(caplog):
    return [record.getMessage() for record in caplog.records
            if record.name == "ui.app" and record.levelno >= logging.WARNING]


def test_a_refused_client_is_logged_with_a_hint(grid, db, cluster_idp, monkeypatch, caplog):
    provider = cluster_idp
    caplog.set_level(logging.WARNING)
    write_token(provider.token_file, provider.cluster, subject="system:serviceaccount:grid:other")
    r = through(grid, provider)
    assert signin_code(r) == "misconfigured" and cookie_set(r, "grid-session") is None
    assert app_warnings(caplog) == [REFUSED + "HTTP 401 invalid_client (error codes 700213). " + SUBJECT_HINT]
    assert count(db, "auditLog") == 0 and count(db, "userSessions") == 0
    # No number: by the method.
    caplog.clear()
    write_token(provider.token_file, fake_idp.FakeCluster(provider.cluster.issuer))
    assert signin_code(through(grid, provider)) == "misconfigured"
    assert app_warnings(caplog) == [REFUSED + "HTTP 401 invalid_client. " + FEDERATED_HINT]
    # Numbers that aren't known here show, and the method's hint with them.
    caplog.clear()
    entra_refusal = fake_idp.entra_refusal
    monkeypatch.setattr(fake_idp, "entra_refusal",
                        lambda error, codes=(), quoted="": entra_refusal(error, (7000215, 50126), quoted))
    assert signin_code(through(grid, provider)) == "misconfigured"
    assert app_warnings(caplog) == [REFUSED + "HTTP 401 invalid_client (error codes 7000215, 50126). " + FEDERATED_HINT]
    assert count(db, "auditLog") == 0 and count(db, "userSessions") == 0
    assert "description-marker" not in caplog.text and fake_idp.TRACE_ID not in caplog.text


def test_a_known_number_is_hinted_under_any_error_code(grid, db, cluster_idp, monkeypatch, caplog):
    """A refused code is no refused client, so the outcome is "failed" and the method has no hint to give; a
    number known here still has."""
    provider = cluster_idp
    caplog.set_level(logging.WARNING)
    for secret in (False, True):
        if secret:
            monkeypatch.setenv("GRID_OIDC_CLIENT_SECRET", fake_idp.CLIENT_SECRET)
            monkeypatch.delenv("GRID_OIDC_CLIENT_AUTH")
            monkeypatch.delenv(OURS)
        for fault, logged in (("token_400_grant_with_number", "HTTP 400 invalid_grant (error codes 700024). " + TIME_HINT),
                              ("token_400_invalid_grant", "HTTP 400 invalid_grant"),
                              ("token_500", "HTTP 500 server_error")):
            caplog.clear()
            provider.fault = fault
            r = through(grid, provider)
            assert signin_code(r) == ("unavailable" if fault == "token_500" else "failed")
            assert app_warnings(caplog) == [REFUSED + logged]
    assert count(db, "userSessions") == 0


# ---- the Admin page's answer -----------------------------------------------------------------


def told(client, **kwargs):
    r = client.get("/api/admin/sign-in", **kwargs)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store", r.get_data(as_text=True)
    return r.get_json()


def test_what_the_admin_page_is_told(grid, db, cluster_idp, monkeypatch, tmp_path):
    provider, path, other = cluster_idp, cluster_idp.token_file, tmp_path / "the-platforms-own"
    exp = 4102444800  # 2100-01-01T00:00:00Z
    write_token(path, provider.cluster, exp=exp)
    write_token(other, provider.cluster, subject="system:serviceaccount:grid:other", exp=exp + 86399)

    def expected(auth, method, token=None, unused=False, problem=None):
        return {"configured": True, "problem": problem, "warning": None, "label": "single sign-on",
                "issuer": provider.base_url, "clientId": "grid-ui", "clientAuth": auth, "clientMethod": method,
                "clientToken": token, "platformTokenUnused": unused, "redirectUri": CALLBACK,
                "scopes": ["openid", "email", "profile"], "groupsClaims": ["groups"], "admins": [],
                "session": {"hours": 12, "idleMinutes": 120}, "readsOpen": False, "adminToken": True,
                "endSession": False}

    def facts(setting, file, **changes):
        return dict({"source": "file", "setting": setting, "file": str(file), "identity": None, "host": None,
                     "endpoint": None, "decoded": True, "issuer": provider.cluster.issuer, "subject": SERVICE_ACCOUNT,
                     "audience": [AUDIENCE], "expiresAt": "2100-01-01T00:00:00Z", "expired": False,
                     "credential": None, "problem": None}, **changes)

    nothing = {"decoded": False, "issuer": None, "subject": None, "audience": [], "expiresAt": None}
    # The platform's token, from each setting; ours is the one read when both are set.
    assert told(grid, headers=ADMIN_TOKEN) == expected("federated", "federated", facts(OURS, path))
    monkeypatch.setenv(THEIRS, str(other))
    assert told(grid, headers=ADMIN_TOKEN) == expected("federated", "federated", facts(OURS, path))
    monkeypatch.delenv(OURS)
    theirs = facts(THEIRS, other, subject="system:serviceaccount:grid:other", expiresAt="2100-01-01T23:59:59Z")
    assert told(grid, headers=ADMIN_TOKEN) == expected("federated", "federated", theirs)
    # Whoever may see people and roles is told the same, admin or not; nobody else is told anything.
    client = grid_app.app.test_client()
    assert client.get("/api/admin/sign-in").status_code == 403
    sign_in_as(client, make_user(db, "Vic Viewer", [permissions.VIEWER]))
    r = client.get("/api/admin/sign-in")
    assert r.status_code == 403 and SERVICE_ACCOUNT not in r.get_data(as_text=True)
    sign_in_as(client, make_user(db, "Rita Reader", [add_role(db, "Readers", "users.view")]))
    assert told(client) == expected("federated", "federated", theirs)
    # A file that holds no token, and one whose token has expired: the problem, and what could be read.
    other.write_bytes(b"not-a-token")
    assert told(grid, headers=ADMIN_TOKEN) == expected(
        "federated", "federated", facts(THEIRS, other, **nothing),
        problem="{} can't be used: {} doesn't hold a token (three base64url parts).".format(THEIRS, other))
    other.unlink()
    assert told(grid, headers=ADMIN_TOKEN) == expected(
        "federated", "federated", facts(THEIRS, other, **nothing),
        problem="{} can't be used: there is no file {}.".format(THEIRS, other))
    write_token(other, provider.cluster, exp=946684800)
    assert told(grid, headers=ADMIN_TOKEN) == expected(
        "federated", "federated", facts(THEIRS, other, expiresAt="2000-01-01T00:00:00Z", expired=True),
        problem="{} can't be used: the token in {} has expired; the platform should have replaced it.".format(
            THEIRS, other))
    # Any other method: nothing of the file, and that the platform's own goes unused.
    write_token(other, provider.cluster, exp=exp)
    for setting in ("auto", "basic", "post"):
        monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", setting)
        assert told(grid, headers=ADMIN_TOKEN) == expected(
            setting, None, unused=True, problem=P3 if setting == "auto" else P5.format(setting))
    monkeypatch.setenv("GRID_OIDC_CLIENT_SECRET", fake_idp.CLIENT_SECRET)
    for setting, method in (("auto", "secret"), ("basic", "basic"), ("post", "post")):
        monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", setting)
        assert told(grid, headers=ADMIN_TOKEN) == expected(setting, method, unused=True)
        assert told(client) == expected(setting, method, unused=True)
    monkeypatch.delenv("GRID_OIDC_CLIENT_AUTH")
    assert told(grid, headers=ADMIN_TOKEN) == expected("auto", "secret", unused=True)
    monkeypatch.delenv(THEIRS)
    assert told(grid, headers=ADMIN_TOKEN) == expected("auto", "secret")
    monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", "federated")
    assert told(grid, headers=ADMIN_TOKEN) == expected("federated", None, problem=P4)
    monkeypatch.setenv(OURS, str(path))
    assert told(grid, headers=ADMIN_TOKEN) == expected("federated", None, problem=P2)
    assert provider.counts["token"] == 0 and provider.assertions == []


def test_claims_shown_are_cleaned(grid, db, cluster_idp):
    """What a token states is text from outside: cut, cleaned and counted before it is shown."""
    provider, markup = cluster_idp, "<img src=x onerror=alert(1)>"
    raw_token(provider.token_file, {
        "iss": " https://oidc.cluster\u0000.example/\u001b[31m\r\n\ud800", "sub": "s" * 300,
        "aud": ["a1", 2, None, "", "a\u0007 3", ["a4"], "\u0000", "a5", "a6"], "exp": 4102444800.75,
        "note": "claim-marker"})
    shown = told(grid, headers=ADMIN_TOKEN)
    assert shown["problem"] is None
    assert shown["clientToken"] == {
        "source": "file", "setting": OURS, "file": str(provider.token_file), "identity": None, "host": None,
        "endpoint": None, "decoded": True, "issuer": "https://oidc.cluster.example/[31m", "subject": "s" * 255,
        "audience": ["a1", "a 3"], "expiresAt": "2100-01-01T00:00:00Z", "expired": False, "credential": None,
        "problem": None}
    raw_token(provider.token_file, {"iss": 7, "sub": markup, "aud": ["a{}".format(n) for n in range(1, 10)]})
    token = told(grid, headers=ADMIN_TOKEN)["clientToken"]
    assert (token["issuer"], token["subject"], token["audience"]) == (None, markup, ["a1", "a2", "a3", "a4", "a5"])
    for audience, shown in ((7, []), ("api://one", ["api://one"]), ({"a": 1}, []), (None, []), ("a" * 300, ["a" * 255])):
        raw_token(provider.token_file, {"iss": ["x"], "sub": {"x": 1}, "aud": audience})
        token = told(grid, headers=ADMIN_TOKEN)["clientToken"]
        assert (token["decoded"], token["issuer"], token["subject"], token["audience"]) == (True, None, None, shown)
    raw_token(provider.token_file, {"iss": "i", "sub": "s", "aud": "a", "note": "claim-marker"})
    r = grid.get("/api/admin/sign-in", headers=ADMIN_TOKEN)
    assert sorted(r.get_json()["clientToken"]) == ["audience", "credential", "decoded", "endpoint", "expired",
                                                   "expiresAt", "file", "host", "identity", "issuer", "problem",
                                                   "setting", "source", "subject"]
    assert "claim-marker" not in r.get_data(as_text=True)


# ---- nothing secret --------------------------------------------------------------------------

FILE_MARKER = b"file-marker-that-is-no-token"


def _holds_a_marker(path, monkeypatch):
    path.write_bytes(FILE_MARKER)


def _states_more(path, monkeypatch):
    raw_token(path, {"iss": "https://oidc.cluster.example/a/", "sub": SERVICE_ACCOUNT, "aud": [AUDIENCE],
                     "exp": int(time.time()) - 3600, "note": "claim-marker"})


def every_way_once(grid, provider, db, monkeypatch):
    """Signs in with a secret and with the platform's token: working, refused by the provider as Entra refuses, and
    failing on each thing that can be wrong with the file and the provider's document. Returns (what must show
    nowhere, every answer of the Grid UI's, the ?signin= code of each attempt)."""
    hidden, responses, codes, path = {fake_idp.CLIENT_SECRET, fake_idp.DESCRIPTION_MARKER, fake_idp.TRACE_ID,
                                      fake_idp.CORRELATION_ID, FILE_MARKER.decode(), "claim-marker",
                                      "an-error-text-nobody-should-read"}, [], [], provider.token_file
    answers, fetch = [], oidc._fetch

    def recorded(url, **kwargs):
        document = fetch(url, **kwargs)
        answers.append(document)
        return document

    monkeypatch.setattr(oidc, "_fetch", recorded)

    def file_token():
        """What the file holds now is hidden: whole, and each part of a token."""
        try:
            text = path.read_bytes().decode("latin-1").strip()
        except OSError:
            return
        if "." in text and len(text) < 20000:
            hidden.update([text] + [part for part in text.split(".") if len(part) > 12])

    def asked():
        return [grid.get(url, headers=ADMIN_TOKEN) for url in ("/api/admin/sign-in", "/api/session",
                                                              "/api/admin/audit", "/api/admin/users?status=all")]

    def attempt(spoil=None, fault=None):
        """One sign-in, spoil() running between its start and its callback."""
        grid_app.reset_login_starts()
        file_token()
        r = start(grid)
        responses.append(r)
        if not r.headers["Location"].startswith(provider.base_url):
            codes.append(signin_code(r))
            return
        hidden.add(run(db, "SELECT codeVerifier FROM oidcLogins ORDER BY id DESC LIMIT 1")[0][0])
        back = provider.approve(r.headers["Location"])
        hidden.add(code_of(back))
        provider.fault = fault
        if spoil:
            spoil()
            file_token()
        r = grid.get(local(back))
        provider.fault = None
        responses.append(r)
        codes.append(signin_code(r))
        if cookie_set(r, "grid-session"):
            responses.extend(asked())
            responses.append(grid.post("/api/session/signout", json={}))

    monkeypatch.setenv("GRID_OIDC_ADMINS", "sub:" + SUBJECT)
    # A client secret, the platform's file beside it unused.
    with monkeypatch.context() as secret:
        secret.setenv("GRID_OIDC_CLIENT_SECRET", fake_idp.CLIENT_SECRET)
        secret.delenv("GRID_OIDC_CLIENT_AUTH")
        secret.delenv(OURS)
        secret.setenv(THEIRS, str(path))
        attempt()
        for fault in ("token_401_entra_70021", "token_400_grant_with_number", "token_401_invalid_client"):
            attempt(fault=fault)
        provider.client_secret = "another-secret"
        attempt()
        provider.client_secret = fake_idp.CLIENT_SECRET
        responses.extend(asked())
    # The platform's token.
    attempt()
    for fault in ("token_401_entra_70021", "token_400_grant_with_number", "token_500"):
        attempt(fault=fault)
    for changes in ({"subject": "system:serviceaccount:grid:other"}, {"audience": "api://AKSIdentityBinding"},
                    {"iss": "https://oidc.elsewhere.example/9/"}, {"exp": int(time.time()) + 30, "nbf": None}):
        write_token(path, provider.cluster, **changes)
        if "exp" in changes:
            provider.clock_offset_s = 60  # expired by the provider's clock, not by ours
        attempt()
        provider.clock_offset_s = 0
        responses.extend(asked())
    write_token(path, fake_idp.FakeCluster(provider.cluster.issuer))
    attempt()
    write_token(path, provider.cluster)
    del provider.federated[:]
    attempt()
    provider.trust(provider.cluster)
    # Each thing wrong with the file: once the sign-in is on its way, and before it starts.
    for _, spoil, _ in FILE_PROBLEMS + [("a marker", _holds_a_marker, ""), ("more claims", _states_more, "")]:
        with monkeypatch.context() as patched:
            attempt(lambda: spoil(path, patched))
            attempt()
            responses.extend(asked())
        if path.is_dir():
            path.rmdir()
        write_token(path, provider.cluster)
    # The provider's document.
    for change in (_no_assertions, _plain_http):
        with monkeypatch.context() as patched:
            attempt(lambda: change(provider, patched))
            attempt()
            responses.extend(asked())
        provider.auth_methods, provider.token_endpoint = list(ALL_METHODS), None
        oidc.reset_caches()
    attempt()
    for document in answers:
        hidden.update(document[k] for k in ("id_token", "access_token", "refresh_token") if k in document)
    return hidden, responses, codes


def shown_in(r):
    """An answer as text: every header (the session cookie's own value left out) and the body."""
    session = cookie_set(r, "grid-session")
    headers = "".join("{}: {}\n".format(k, v) for k, v in r.headers.items())
    if session and session["value"]:
        headers = headers.replace(session["value"], "")
    return headers + r.get_data(as_text=True)


def test_nothing_secret_reaches_an_answer_a_row_or_the_log(grid, db, cluster_idp, monkeypatch, caplog):
    """The client secret, the platform's token (whole and each part), whatever else its file holds, the code, the
    PKCE verifier, the provider's tokens and every word of the provider's own in a refusal: in no log line at any
    level, no answer and no audit row, however the sign-in goes."""
    provider = cluster_idp
    caplog.set_level(logging.DEBUG)
    hidden, responses, codes = every_way_once(grid, provider, db, monkeypatch)
    # The run did go each way: sign-ins worked, the provider refused with numbers, and every file problem was met.
    assert codes.count(None) == 3 and len([value for value in hidden if value.startswith("at-")]) == 3
    assert {"misconfigured", "failed", "unavailable"} <= set(codes)
    assert provider.auth_used.count("federated") >= 8 and "client_secret_basic" in provider.auth_used
    assert len(hidden) > 100 and len(responses) > 150
    logged = "".join("{}\n{}\n".format(record.getMessage(), record.exc_text or "") for record in caplog.records)
    logged += caplog.text
    for number in ("70021", "700024", "700213", "700212", "700211", "70025"):
        assert "error codes {})".format(number) in logged
    assert str(provider.token_file) in logged  # a path may be logged; what the file holds may not
    rows = "\n".join(repr(row) for row in run(db, "SELECT * FROM auditLog"))
    assert "auth.signin" in rows
    answered = "".join(shown_in(r) for r in responses)
    assert SERVICE_ACCOUNT in answered and provider.cluster.issuer in answered  # the claims an admin is shown
    for value in hidden:
        assert value and value not in logged, "logged"
        assert value not in answered, "answered"
        assert value not in rows, "kept in the audit log"
    # This change writes no audit row: every one is of a kind written before it.
    assert {action for (action,) in run(db, "SELECT DISTINCT action FROM auditLog")} <= {
        "auth.signin", "auth.signout", "auth.breakglass", "user.created"}


P2_ANY = (re.escape("Set only one of GRID_OIDC_CLIENT_SECRET, GRID_OIDC_FEDERATED_TOKEN_FILE and "
                     "GRID_OIDC_MANAGED_IDENTITY: ") + r"(GRID_OIDC_\w+, )?GRID_OIDC_\w+ and GRID_OIDC_\w+"
          + re.escape(" are set, and the Grid UI won't pick one."))
P6_ANY = (r"(GRID_OIDC_FEDERATED_TOKEN_FILE|GRID_OIDC_MANAGED_IDENTITY)"
          + re.escape(" is used only with GRID_OIDC_CLIENT_AUTH=federated: set that, or unset ")
          + r"(GRID_OIDC_FEDERATED_TOKEN_FILE|GRID_OIDC_MANAGED_IDENTITY)\.")
P8 = ("GRID_OIDC_MANAGED_IDENTITY is system, or the client id of a user-assigned managed identity (a GUID such as "
      "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee).")
P9 = ("GRID_OIDC_MANAGED_IDENTITY doesn't go with the AZURE_FEDERATED_TOKEN_FILE that Azure workload identity sets: in a "
      "Kubernetes pod that file is the Grid UI's own identity, and the managed identity endpoint gives out the node's. "
      "Unset GRID_OIDC_MANAGED_IDENTITY.")
P10 = ("GRID_OIDC_MANAGED_IDENTITY needs an Entra ID GRID_OIDC_ISSUER over https "
       "(https://login.microsoftonline.com/<tenant id>/v2.0, or login.microsoftonline.us or login.chinacloudapi.cn in "
       "those clouds): a managed identity's token is good only there, and is sent nowhere else.")
# P11 and M1 to M11: what keeps the managed identity endpoint from giving a token (test_managed_identity.py has each).
MANAGED_IDENTITY = r"GRID_OIDC_MANAGED_IDENTITY can't be used: [^\n]+\."
X2 = (re.escape("the discovery document's token_endpoint isn't https on ") + r"\S+"
      + re.escape(", GRID_OIDC_ISSUER's host, and a managed identity's token is sent nowhere else"))
PROBLEMS = [re.escape(text) for text in (P1, P3, P4, P5.format("basic"), P5.format("post"), P5_FEDERATED, P8, P9,
                                         P10, HTTPS_ISSUER)] + [P2_ANY, P6_ANY, MANAGED_IDENTITY] + [
    r"(GRID_OIDC|AZURE)_FEDERATED_TOKEN_FILE can't be used: (" + "|".join(
        re.escape(reason.replace("{p}", "\0")).replace("\0", r"\S+")
        for reason in sorted({row[2] for row in FILE_PROBLEMS})) + r")\."]
MISMATCHES = [re.escape(NO_ASSERTIONS), re.escape(PLAIN_HTTP), X2,
              re.escape("the discovery document's token_endpoint isn't an https URL")]
HINTS = [re.escape("AADSTS{}: {}".format(number, hint)) for number, hint in oidc._ENTRA_HINTS.items()] + [
    re.escape(FEDERATED_HINT), re.escape(MANAGED_IDENTITY_HINT), re.escape(SECRET_HINT)]
# Each line the change adds to or alters (L1 to L4), and under BEFORE the lines of before it that the run meets.
LISTED = {
    "ui.oidc": ["|".join(PROBLEMS + [re.escape(W1)])],  # L1
    "ui.app": [
        "couldn't start a sign-in: (" + "|".join(MISMATCHES + PROBLEMS) + ")",  # L2
        re.escape(REFUSED) + r"HTTP \d{3}( [A-Za-z0-9_.-]{1,64})?( \(error codes \d{1,9}(, \d{1,9}){0,3}\))?(\. ("
        + "|".join(HINTS) + "))?",  # L3
        "couldn't redeem a sign-in's code: (" + "|".join(MISMATCHES + PROBLEMS) + ")",  # L4
    ],
    "ui.store": [],
}
BEFORE = {
    "ui.oidc": [re.escape(GENERAL_HTTP)],
    "ui.app": [],
    "ui.store": [],
}
NEW_FUNCTIONS = ("_small_file", "_federated_file", "_token_claims", "_expiry", "_utc", "_federated_token",
                 "_client_method", "client_method", "platform_token_unused", "federated_token_facts", "check_client",
                 "_error_codes", "refusal_hint", "hint_method", "provider_problem", "_client_auth_method", "redeem",
                 # and those that bring the managed identity and plain http
                 "_entra_audience", "_local_address", "_endpoint_text", "_identity_endpoint", "_where",
                 "_identity_fetch", "_unreached", "_identity_named", "_managed_identity_token", "_identity_key",
                 "_remember", "_remembered", "_identity_outcome", "_platform_token", "_check_provider",
                 "plain_http_warning")


def listed_lines_only(caplog):
    """Asserts every line at WARNING or above of the Grid UI's is one of LISTED or BEFORE, without a traceback, and
    that the functions the change adds don't log. Returns the (logger, pattern number) of each line met."""
    met = set()
    for record in caplog.records:
        if record.levelno < logging.WARNING or record.name not in LISTED:
            continue
        line = record.getMessage()
        found = [n for n, pattern in enumerate(LISTED[record.name] + BEFORE[record.name])
                 if re.fullmatch(pattern, line)]
        assert found, (record.name, line)
        assert record.exc_info is None and record.exc_text is None, line
        met.add((record.name, found[0]))
    # No other logger of the Grid UI's says anything at that level.
    assert {record.name for record in caplog.records if record.levelno >= logging.WARNING
            and record.name.startswith("ui.")} <= set(LISTED)
    # The functions the change adds return or raise: none of them logs. problem() does, as before.
    for name in NEW_FUNCTIONS:
        source = inspect.getsource(getattr(oidc, name))
        assert "log." not in source and "warn_once" not in source and "print(" not in source, name
    assert inspect.getsource(oidc.problem).count("warn_once") == 2
    return met


def test_only_the_listed_lines_are_logged(grid, db, cluster_idp, monkeypatch, caplog):
    provider = cluster_idp
    caplog.set_level(logging.DEBUG)
    every_way_once(grid, provider, db, monkeypatch)
    met = listed_lines_only(caplog)
    assert {("ui.oidc", 0), ("ui.app", 0), ("ui.app", 1), ("ui.app", 2)} <= met  # L1 to L4 were each written


# ---- secret methods unchanged ----------------------------------------------------------------

AWKWARD = "p:a%2Fss w+rd/é~"


def sent_before(setting, listed, code, verifier):
    """The token request of the release before this change (d05b719), written out: (form, headers)."""
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": verifier,
            "client_id": fake_idp.CLIENT_ID}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if setting == "basic" or setting != "post" and (
            not isinstance(listed, list) or not listed or "client_secret_basic" in listed):
        pair = urllib.parse.quote(fake_idp.CLIENT_ID, safe="") + ":" + urllib.parse.quote(AWKWARD, safe="")
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode("ascii")
    else:
        form["client_secret"] = AWKWARD
    return form, headers


@pytest.mark.parametrize("platform_file", [False, True])
@pytest.mark.parametrize("listed", [None, [], ["client_secret_basic", "client_secret_post"], ["client_secret_post"],
                                    ["client_secret_basic"], ["private_key_jwt"],
                                    ["client_secret_post", "private_key_jwt"], ALL_METHODS, "client_secret_post"])
@pytest.mark.parametrize("setting", [None, "auto", "basic", "post", "AUTO", " Post "])
def test_a_secret_install_sends_what_it_sent(idp, monkeypatch, tmp_path, setting, listed, platform_file):
    monkeypatch.setenv("GRID_OIDC_CLIENT_SECRET", AWKWARD)
    if setting is not None:
        monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", setting)
    if platform_file:
        write_token(tmp_path / "token", fake_idp.FakeCluster())
        monkeypatch.setenv(THEIRS, str(tmp_path / "token"))
    document = dict(oidc.discovery())
    document.pop("token_endpoint_auth_methods_supported", None)
    if listed is not None:
        document["token_endpoint_auth_methods_supported"] = listed
    monkeypatch.setattr(oidc, "discovery", lambda: document)
    sent, opened = [], opened_files(monkeypatch)

    def answered(url, data=None, headers=None, **kwargs):
        sent.append((url, data, headers, kwargs))
        return {"id_token": "made-up", "access_token": "dropped"}

    monkeypatch.setattr(oidc, "_fetch", answered)
    assert oidc.problem() is None
    oidc.check_client()
    assert oidc.redeem("a code/+&=", "a-verifier") == "made-up"
    form, headers = sent_before((setting or "auto").strip().lower(), listed, "a code/+&=", "a-verifier")
    assert sent == [(idp.base_url + "/token", urllib.parse.urlencode(form).encode(), headers,
                     {"timeout": oidc.TOKEN_TIMEOUT_S, "limit": oidc.TOKEN_LIMIT})]
    assert opened == []
