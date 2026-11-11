"""Signing in with an Azure managed identity's token in place of a client secret: the Grid UI asks the platform's
identity endpoint (an Azure VM's Instance Metadata Service, or App Service's, Functions' and Container Apps'
IDENTITY_ENDPOINT) for its managed identity's token and sends that as its client assertion. A fake endpoint and the
fake provider, standing in for Entra on loopback, play both sides; nothing here reaches Azure.
Run from the repo root: python -m pytest ui/tests"""
import base64
import inspect
import json
import logging
import pathlib
import time

import pytest

from ui import app as grid_app, oidc, permissions, store
from ui.tests import fake_idp
from ui.tests.test_accounts import add_role, db, make_user, run, sign_in_as  # noqa: F401  (db is a fixture)
from ui.tests.test_client_auth import (ADMIN_TOKEN, ALL_METHODS, AUDIENCE, LOCAL_ENDPOINTS, MANAGED_IDENTITY_HINT,
                                       OURS, P1, P3, P4, P5, P5_FEDERATED, P8, P9, P10, REFUSED, REFUSED_ENDPOINTS,
                                       THEIRS, _held, app_warnings,
                                       listed_lines_only, non_loopback_address, proxied_through, shown_in, told,
                                       token_requests, write_token)
from ui.tests.test_oidc import (CALLBACK, GOOD, code_of, configure, cookie_set, count, grid, idp, local,  # noqa: F401
                                signin_code, start, stopped_clock, through)

SYSTEM_OBJECT_ID = "eeeeeeee-1111-2222-3333-444444444444"
OTHER_CLIENT_ID = "abababab-1111-2222-3333-444444444444"
OTHER_OBJECT_ID = "cdcdcdcd-5555-6666-7777-888888888888"
KINDS = ["imds", "app-service"]
IMDS_WHERE = "the Azure Instance Metadata Service (http://169.254.169.254)"
CANT = "GRID_OIDC_MANAGED_IDENTITY can't be used: {}."
START, REDEEM = "couldn't start a sign-in: ", "couldn't redeem a sign-in's code: "
API_VERSIONS = {"imds": "2018-02-01", "app-service": "2019-08-01"}


@pytest.fixture
def endpoint():
    """A fake managed identity endpoint on loopback, answering both ways Azure offers one."""
    found = fake_idp.serve_identity()
    yield found
    found.stop()


def point_at(endpoint, kind, monkeypatch):
    """The environment as that kind of platform sets it, aimed at the fake: a VM (no IDENTITY_ENDPOINT, IMDS_URL at
    the fake) or App Service (IDENTITY_ENDPOINT and IDENTITY_HEADER)."""
    if kind == "imds":
        monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
        monkeypatch.delenv("IDENTITY_HEADER", raising=False)
        monkeypatch.setattr(oidc, "IMDS_URL", endpoint.imds_url)
    else:
        monkeypatch.setenv("IDENTITY_ENDPOINT", endpoint.app_service_url)
        monkeypatch.setenv("IDENTITY_HEADER", endpoint.header)


def managed(provider, endpoint, monkeypatch, kind="app-service", identity="user", entra=True):
    """The provider stands in for Entra (its loopback host added to ENTRA_AUDIENCES, unless not entra), takes client
    assertions and trusts the endpoint's tenant for the user-assigned and the system-assigned identity; the settings
    send the managed identity's token (identity: user or system), from that kind of endpoint."""
    if entra:
        monkeypatch.setitem(oidc.ENTRA_AUDIENCES, "127.0.0.1", AUDIENCE)
    provider.auth_methods = list(ALL_METHODS)
    endpoint.system = SYSTEM_OBJECT_ID
    provider.trust(endpoint.tenant, subject=fake_idp.OBJECT_ID_MI)
    provider.trust(endpoint.tenant, subject=SYSTEM_OBJECT_ID)
    for name in ("GRID_OIDC_CLIENT_SECRET", OURS, THEIRS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", "federated")
    monkeypatch.setenv("GRID_OIDC_MANAGED_IDENTITY", "system" if identity == "system" else fake_idp.CLIENT_ID_MI)
    point_at(endpoint, kind, monkeypatch)
    return provider


def where(endpoint, kind):
    return IMDS_WHERE if kind == "imds" else "the managed identity endpoint in IDENTITY_ENDPOINT ({})".format(
        endpoint.app_service_url)


def claims_of(token):
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def logins(db_path):
    return count(db_path, "oidcLogins")


# ---- which method, and which endpoint ---------------------------------------------------------------

ENTRA = fake_idp.TENANT_ISSUER
MI = fake_idp.CLIENT_ID_MI


def P2(*names):
    return ("Set only one of GRID_OIDC_CLIENT_SECRET, GRID_OIDC_FEDERATED_TOKEN_FILE and GRID_OIDC_MANAGED_IDENTITY: "
            "{} and {} are set, and the Grid UI won't pick one.".format(", ".join(names[:-1]), names[-1]))


S, F, M = "GRID_OIDC_CLIENT_SECRET", "GRID_OIDC_FEDERATED_TOKEN_FILE", "GRID_OIDC_MANAGED_IDENTITY"
P6_M = "GRID_OIDC_MANAGED_IDENTITY is used only with GRID_OIDC_CLIENT_AUTH=federated: set that, or unset GRID_OIDC_MANAGED_IDENTITY."


# setting, S, F, M, Z, issuer, more, method, problem (of _client_method)
@pytest.mark.parametrize("setting, secret, ours, mi, theirs, issuer, more, method, problem", [
    ("auto", True, False, MI, False, ENTRA, {}, None, P2(S, M)),
    ("federated", True, False, MI, True, ENTRA, {}, None, P2(S, M)),
    ("federated", False, True, MI, False, ENTRA, {}, None, P2(F, M)),
    ("basic", True, True, MI, False, ENTRA, {}, None, P2(S, F, M)),
    ("federated", True, True, MI, True, ENTRA, {}, None, P2(S, F, M)),
    ("auto", False, False, MI, False, ENTRA, {}, None, P6_M),
    ("auto", False, False, MI, True, ENTRA, {}, None, P6_M),
    ("basic", False, False, MI, False, ENTRA, {}, None, P6_M),
    ("post", False, False, "system", True, ENTRA, {}, None, P6_M),
    ("auto", True, False, None, False, ENTRA, {}, "secret", None),
    ("federated", True, False, None, False, ENTRA, {}, None, P4),
    ("federated", False, False, None, False, ENTRA, {}, None, P5_FEDERATED),
    ("auto", False, False, None, False, ENTRA, {}, None, P3),
    ("basic", False, False, None, False, ENTRA, {}, None, P5.format("basic")),
    ("federated", False, False, "sytem", False, ENTRA, {}, None, P8),
    ("federated", False, False, "sytem", True, "https://login.example.com", {}, None, P8),
    ("federated", False, False, MI, True, ENTRA, {}, None, P9),
    ("federated", False, False, MI, True, "https://login.example.com", {}, None, P9),
    ("federated", False, False, MI, False, "https://login.example.com", {}, None, P10),
    ("federated", False, False, MI, False, "https://accounts.google.com", {}, None, P10),
    ("federated", False, False, MI, False, "http://login.microsoftonline.com/" + fake_idp.TENANT_ID + "/v2.0",
     {"GRID_OIDC_ALLOW_HTTP": "1"}, None, P10),
    ("federated", False, False, MI, False, "https://login.microsoftonline.com.example.net/t/v2.0", {}, None, P10),
    ("federated", False, False, MI, False, ENTRA, {}, "federated", None),
    ("FEDERATED", False, False, "system", False, ENTRA, {}, "federated", None),
    ("federated", False, False, MI, False, "https://login.microsoftonline.us/t/v2.0", {}, "federated", None),
    ("federated", False, False, MI, False, "https://login.chinacloudapi.cn/t/v2.0", {}, "federated", None),
    ("jwt", False, False, MI, False, ENTRA, {}, None, P1),
])
def test_what_each_setting_comes_to_with_a_managed_identity(monkeypatch, tmp_path, endpoint, setting, secret, ours,
                                                            mi, theirs, issuer, more, method, problem):
    """Every row with a managed identity, by the settings alone: nothing is asked, whatever the row."""
    usable = tmp_path / "token"
    write_token(usable, fake_idp.FakeCluster())
    configure(monkeypatch, GRID_OIDC_ISSUER=issuer, GRID_OIDC_CLIENT_AUTH=setting,
              GRID_OIDC_CLIENT_SECRET=GOOD["GRID_OIDC_CLIENT_SECRET"] if secret else None,
              GRID_OIDC_MANAGED_IDENTITY=mi, **dict(more, **{OURS: str(usable) if ours else None,
                                                             THEIRS: str(usable) if theirs else None}))
    monkeypatch.setattr(oidc, "IMDS_URL", endpoint.imds_url)
    assert oidc._client_method(oidc.settings()) == (method, problem)
    assert oidc.client_method() == method
    assert oidc.problem() == problem
    assert endpoint.requests == []


@pytest.mark.parametrize("written, read", [("SYSTEM", "system"), (" System ", "system"), ("system", "system"),
                                           (MI.upper(), MI), (" " + MI + "\n", MI)])
def test_the_setting_is_read_as_written(monkeypatch, written, read):
    configure(monkeypatch, GRID_OIDC_ISSUER=ENTRA, GRID_OIDC_CLIENT_SECRET=None, GRID_OIDC_CLIENT_AUTH="federated",
              GRID_OIDC_MANAGED_IDENTITY=written)
    assert oidc.settings().managed_identity == read
    assert oidc._client_method(oidc.settings()) == ("federated", None)


@pytest.mark.parametrize("written", ["{" + MI + "}", MI + "&resource=x", "system," + MI, MI[:-1], MI + "a",
                                     MI[:-1] + "g", "grid-ui", fake_idp.OBJECT_ID_MI.replace("b", "x", 1),
                                     "/subscriptions/s/resourcegroups/g/providers/x/grid-ui"])
def test_anything_else_is_no_identity(monkeypatch, written):
    configure(monkeypatch, GRID_OIDC_ISSUER=ENTRA, GRID_OIDC_CLIENT_SECRET=None, GRID_OIDC_CLIENT_AUTH="federated",
              GRID_OIDC_MANAGED_IDENTITY=written)
    assert len(MI[:-1]) == 35 and len(MI + "a") == 37
    assert oidc.problem() == P8 and oidc.client_method() is None


@pytest.mark.parametrize("issuer, audience", [
    ("https://login.microsoftonline.com/t/v2.0", "api://AzureADTokenExchange"),
    ("https://LOGIN.MICROSOFTONLINE.COM/t/v2.0", "api://AzureADTokenExchange"),
    ("https://login.microsoftonline.us/t/v2.0", "api://AzureADTokenExchangeUSGov"),
    ("https://login.chinacloudapi.cn/t/v2.0", "api://AzureADTokenExchangeChina"),
    ("http://login.microsoftonline.com/t/v2.0", None),
    ("https://login.microsoftonline.com.example.net/t/v2.0", None),
    ("https://login.partner.microsoftonline.cn/t/v2.0", None),
    ("https://dev-123.okta.com", None),
    ("https://accounts.google.com", None),
    ("http://keycloak:8080/realms/grid", None),
    ("https://keycloak.example.com/realms/grid", None),
    ("https://t.ciamlogin.com/t/v2.0", None),
    ("", None),
    ("login.microsoftonline.com", None),
])
@pytest.mark.parametrize("allow_http", [None, "1"])
def test_a_managed_identity_goes_only_to_entra(monkeypatch, issuer, audience, allow_http):
    configure(monkeypatch, GRID_OIDC_ISSUER=issuer or None, GRID_OIDC_CLIENT_SECRET=None,
              GRID_OIDC_CLIENT_AUTH="federated", GRID_OIDC_MANAGED_IDENTITY=MI, GRID_OIDC_ALLOW_HTTP=allow_http)
    assert oidc._entra_audience(issuer) == audience
    assert oidc._client_method(oidc.settings()) == (("federated", None) if audience else (None, P10))


SERVICE = "http://127.0.0.1:8081/msi/token"
WHERE_NOT_LOCAL = ("IDENTITY_ENDPOINT is {}, and the Grid UI asks for a managed identity's token only over plain http "
                   "at a local address (localhost, or a loopback, link-local or private IP address), with no query")
CLAUSES = {
    "fabric": ("IDENTITY_SERVER_THUMBPRINT is set, as Service Fabric sets it, and Service Fabric's managed identity "
               "isn't supported"),
    "arc": ("IDENTITY_ENDPOINT and IMDS_ENDPOINT are set without IDENTITY_HEADER, as Azure Arc sets them, and an Azure "
            "Arc server's managed identity isn't supported"),
    "no header": ("IDENTITY_ENDPOINT is set without the IDENTITY_HEADER that App Service, Functions and Container Apps "
                  "set beside it"),
    "header": "IDENTITY_HEADER isn't a usable header value",
    "unreadable": ("IDENTITY_ENDPOINT isn't an address the Grid UI can read (check the host, any [ ] around an IPv6 "
                   "address, and the port)"),
    "msi": ("MSI_ENDPOINT or MSI_SECRET is set without IDENTITY_ENDPOINT, and that older managed identity endpoint isn't "
            "supported"),
    "no endpoint": ("this is App Service, Functions or Container Apps, but IDENTITY_ENDPOINT isn't set: turn the app's "
                    "managed identity on, then restart it"),
    "pod": ("this is a Kubernetes pod, where the Instance Metadata Service gives out the node's identity, not the Grid "
            "UI's: use the pod's token file instead"),
}
ENDPOINT_ROWS = [
    ("1", {"IDENTITY_SERVER_THUMBPRINT": "AB12", "IDENTITY_ENDPOINT": SERVICE, "IDENTITY_HEADER": "h"}, CLAUSES["fabric"]),
    ("1, no endpoint", {"IDENTITY_SERVER_THUMBPRINT": "AB12"}, CLAUSES["fabric"]),
    ("2", {"IDENTITY_ENDPOINT": "http://localhost:40342/metadata/identity/oauth2/token",
           "IMDS_ENDPOINT": "http://localhost:40342"}, CLAUSES["arc"]),
    ("3", {"IDENTITY_ENDPOINT": SERVICE}, CLAUSES["no header"]),
    ("3, a blank header", {"IDENTITY_ENDPOINT": SERVICE, "IDENTITY_HEADER": ""}, CLAUSES["no header"]),
    ("4", {"IDENTITY_ENDPOINT": SERVICE, "IDENTITY_HEADER": "a b"}, CLAUSES["header"]),
    ("5, a query", {"IDENTITY_ENDPOINT": SERVICE + "?object_id=x", "IDENTITY_HEADER": "h"},
     WHERE_NOT_LOCAL.format(SERVICE)),
    ("5, a fragment", {"IDENTITY_ENDPOINT": SERVICE + "#x", "IDENTITY_HEADER": "h"}, WHERE_NOT_LOCAL.format(SERVICE)),
    ("5, a path", {"IDENTITY_ENDPOINT": SERVICE + "/<x>", "IDENTITY_HEADER": "h"},
     WHERE_NOT_LOCAL.format(SERVICE + "/<x>")),
    ("6", {"IDENTITY_ENDPOINT": "http://8.8.8.8/msi/token", "IDENTITY_HEADER": "h"},
     WHERE_NOT_LOCAL.format("http://8.8.8.8/msi/token")),
    ("6, a user", {"IDENTITY_ENDPOINT": "http://user:pw@127.0.0.1/msi/token", "IDENTITY_HEADER": "h"},
     WHERE_NOT_LOCAL.format("http://127.0.0.1/msi/token")),
    ("6, an unclosed [", {"IDENTITY_ENDPOINT": "http://[fe80::1/msi/token", "IDENTITY_HEADER": "h"},
     CLAUSES["unreadable"]),
    ("6, a port too high", {"IDENTITY_ENDPOINT": "http://127.0.0.1:99999/msi/token", "IDENTITY_HEADER": "h"},
     CLAUSES["unreadable"]),
    ("6, a port that isn't a number", {"IDENTITY_ENDPOINT": "http://127.0.0.1:abc/msi/token", "IDENTITY_HEADER": "h"},
     CLAUSES["unreadable"]),
    ("7",{"IDENTITY_ENDPOINT": SERVICE, "IDENTITY_HEADER": "h"}, ("app-service", SERVICE)),
    ("7, in a pod", {"IDENTITY_ENDPOINT": SERVICE, "IDENTITY_HEADER": "h", "KUBERNETES_SERVICE_HOST": "10.0.0.1",
                     "CONTAINER_APP_NAME": "grid"}, ("app-service", SERVICE)),
    ("7, App Service on Linux", {"IDENTITY_ENDPOINT": "http://172.16.1.5:8081/msi/token", "IDENTITY_HEADER": "h",
                                 "WEBSITE_SITE_NAME": "grid", "MSI_ENDPOINT": "x"},
     ("app-service", "http://172.16.1.5:8081/msi/token")),
    ("7, IPv6", {"IDENTITY_ENDPOINT": "http://[::1]:8081/msi/token", "IDENTITY_HEADER": "h"},
     ("app-service", "http://[::1]:8081/msi/token")),
    ("7, upper case", {"IDENTITY_ENDPOINT": "HTTP://LOCALHOST:41/msi/token", "IDENTITY_HEADER": "h"},
     ("app-service", "http://localhost:41/msi/token")),
    ("8, MSI_ENDPOINT", {"MSI_ENDPOINT": "http://127.0.0.1:41741/MSI/token/"}, CLAUSES["msi"]),
    ("8, MSI_SECRET", {"MSI_SECRET": "s"}, CLAUSES["msi"]),
    ("9, App Service", {"WEBSITE_SITE_NAME": "grid"}, CLAUSES["no endpoint"]),
    ("9, Container Apps", {"CONTAINER_APP_NAME": "grid", "KUBERNETES_SERVICE_HOST": "10.0.0.1"}, CLAUSES["no endpoint"]),
    ("10", {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, CLAUSES["pod"]),
    ("11", {}, ("imds", None)),
]


@pytest.mark.parametrize("allow_http", [None, "1"])
@pytest.mark.parametrize("row, env, expected", ENDPOINT_ROWS, ids=[row[0] for row in ENDPOINT_ROWS])
def test_which_endpoint(monkeypatch, row, env, expected, allow_http):
    """D30's rows, in order: the first that matches decides. GRID_OIDC_ALLOW_HTTP changes nothing."""
    configure(monkeypatch, GRID_OIDC_ISSUER=ENTRA, GRID_OIDC_CLIENT_SECRET=None, GRID_OIDC_CLIENT_AUTH="federated",
              GRID_OIDC_MANAGED_IDENTITY=MI, GRID_OIDC_ALLOW_HTTP=allow_http, **env)
    found, why = oidc._identity_endpoint()
    if isinstance(expected, tuple):
        kind, url = expected
        assert why is None and (found.kind, found.url) == (kind, url or oidc.IMDS_URL)
        assert (found.api_version, found.header) == ((oidc.IMDS_API_VERSION, "Metadata") if kind == "imds" else
                                                     (oidc.APP_SERVICE_API_VERSION, "X-IDENTITY-HEADER"))
        assert oidc.problem() is None
    else:
        assert (found, why) == (None, expected)
        assert oidc.problem() == CANT.format(expected)
        if row == "6, a user":
            assert "pw" not in why and "user" not in why
        assert "None" not in why and "http:///" not in why
        assert oidc.federated_token_facts()["endpoint"] is None


def test_the_vms_address_is_local():
    assert oidc._local_address("http://169.254.169.254/metadata/identity/oauth2/token") == (
        "169.254.169.254", None, "/metadata/identity/oauth2/token")
    assert oidc.IMDS_URL != "http://169.254.169.254/metadata/identity/oauth2/token"  # the guard stands in for it


@pytest.mark.parametrize("allow_http", [None, "1"])
def test_which_addresses_are_local(monkeypatch, allow_http):
    configure(monkeypatch, GRID_OIDC_ALLOW_HTTP=allow_http)
    for address in LOCAL_ENDPOINTS:
        assert oidc._local_address(address), address
    for address in REFUSED_ENDPOINTS:
        assert oidc._local_address(address.format(port=8081)) is None, address


@pytest.mark.parametrize("value", ["hdr-marker\nb", " hdr-marker", "hdr-marker ", "hdr marker", "h" * 4097,
                                   "hdr-markeré", "hdr-marker\t", "hdr-marker\x7f"])
def test_identity_header_is_checked_and_never_shown(grid, db, idp, endpoint, monkeypatch, caplog, value):
    caplog.set_level(logging.DEBUG)
    managed(idp, endpoint, monkeypatch)
    monkeypatch.setenv("IDENTITY_HEADER", value)
    sentence = CANT.format(CLAUSES["header"])
    assert oidc.problem() == sentence
    r = start(grid)
    assert signin_code(r) == "misconfigured"
    answers = [grid.get("/api/session", headers=ADMIN_TOKEN), grid.get("/api/admin/sign-in", headers=ADMIN_TOKEN)]
    assert answers[1].get_json()["problem"] == sentence
    assert endpoint.requests == []
    shown = "".join(shown_in(a) for a in answers + [r]) + caplog.text
    assert value.strip() not in shown
    # The longest that is taken, and is sent as it is.
    monkeypatch.setenv("IDENTITY_HEADER", "a" * 4096)
    assert oidc.problem() is None


@pytest.mark.parametrize("env", [{"MSI_ENDPOINT": "http://127.0.0.1:41741/MSI/token/"}, {"MSI_SECRET": "s"},
                                 {"WEBSITE_SITE_NAME": "grid"}, {"CONTAINER_APP_NAME": "grid"},
                                 {"KUBERNETES_SERVICE_HOST": "10.0.0.1"}])
def test_no_fallback_to_the_vms_address(grid, db, idp, endpoint, monkeypatch, env):
    """The environment says the identity is elsewhere: refused, and the VM's address (here, the fake) never asked.
    conftest's guard would fail the test if the real one's stand-in were asked."""
    managed(idp, endpoint, monkeypatch, kind="imds")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    sentence = oidc.problem()
    assert sentence.startswith("GRID_OIDC_MANAGED_IDENTITY can't be used: ")
    assert signin_code(start(grid)) == "misconfigured"
    assert told(grid, headers=ADMIN_TOKEN)["problem"] == sentence
    assert grid.get("/api/session", headers=ADMIN_TOKEN).status_code == 200
    assert endpoint.requests == []


# ---- never asked unless the settings say so ----------------------------------------------------------


def _set(**env):
    def change(provider, endpoint, monkeypatch, tmp_path):
        for name, value in env.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
    return change


def _token_file(name, **env):
    def change(provider, endpoint, monkeypatch, tmp_path):
        cluster = fake_idp.FakeCluster()
        provider.trust(cluster)
        write_token(tmp_path / "token", cluster)
        monkeypatch.setenv(name, str(tmp_path / "token"))
        _set(**env)(provider, endpoint, monkeypatch, tmp_path)
    return change


def _not_entra(provider, endpoint, monkeypatch, tmp_path):
    monkeypatch.delitem(oidc.ENTRA_AUDIENCES, "127.0.0.1")


def _endpoint_with_a_query(provider, endpoint, monkeypatch, tmp_path):
    if "IDENTITY_ENDPOINT" in oidc.os.environ:
        monkeypatch.setenv("IDENTITY_ENDPOINT", endpoint.app_service_url + "?object_id=x")
    else:  # a VM's: a pod has the VM's address, which is refused
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")


NO_M = {"GRID_OIDC_MANAGED_IDENTITY": None}
NEVER_ASKED = [
    ("auto, no secret (P3)", _set(GRID_OIDC_CLIENT_AUTH="auto", **NO_M)),
    ("auto with a secret", _set(GRID_OIDC_CLIENT_AUTH="auto", GRID_OIDC_CLIENT_SECRET=fake_idp.CLIENT_SECRET, **NO_M)),
    ("basic", _set(GRID_OIDC_CLIENT_AUTH="basic", GRID_OIDC_CLIENT_SECRET=fake_idp.CLIENT_SECRET, **NO_M)),
    ("post", _set(GRID_OIDC_CLIENT_AUTH="post", GRID_OIDC_CLIENT_SECRET=fake_idp.CLIENT_SECRET, **NO_M)),
    ("a token file", _token_file(OURS, **NO_M)),
    ("the platform's token file", _token_file(THEIRS, **NO_M)),
    ("P1", _set(GRID_OIDC_CLIENT_AUTH="jwt")),
    ("P2", _set(GRID_OIDC_CLIENT_SECRET=fake_idp.CLIENT_SECRET)),
    ("P4", _set(GRID_OIDC_CLIENT_SECRET=fake_idp.CLIENT_SECRET, **NO_M)),
    ("P5", _set(**NO_M)),
    ("P6", _set(GRID_OIDC_CLIENT_AUTH="auto")),
    ("P8", _set(GRID_OIDC_MANAGED_IDENTITY="sytem")),
    ("P9", _token_file(THEIRS)),
    ("P10", _not_entra),
    ("P11", _endpoint_with_a_query),
]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case, change", NEVER_ASKED, ids=[row[0] for row in NEVER_ASKED])
def test_a_managed_identity_is_never_asked_by_itself(grid, db, idp, endpoint, monkeypatch, tmp_path, kind, case, change):
    """With the endpoint answering, every setting but GRID_OIDC_CLIENT_AUTH=federated with a usable
    GRID_OIDC_MANAGED_IDENTITY leaves it alone: through the session, a start, a forced callback and the Admin page."""
    managed(idp, endpoint, monkeypatch, kind=kind)
    # A sign-in on its way, started with a secret, so that only its callback meets the settings under test.
    with monkeypatch.context() as secret:
        secret.setenv("GRID_OIDC_CLIENT_SECRET", fake_idp.CLIENT_SECRET)
        secret.setenv("GRID_OIDC_CLIENT_AUTH", "auto")
        secret.delenv("GRID_OIDC_MANAGED_IDENTITY")
        back = idp.approve(start(grid).headers["Location"])
    change(idp, endpoint, monkeypatch, tmp_path)
    if case.startswith("P"):
        assert oidc.problem()
    grid.get("/api/session", headers=ADMIN_TOKEN)
    grid.get(local(back))
    start(grid)
    told(grid, headers=ADMIN_TOKEN)
    assert endpoint.requests == []


@pytest.mark.parametrize("kind", KINDS)
def test_problem_never_asks_the_endpoint(grid, db, idp, endpoint, monkeypatch, kind):
    managed(idp, endpoint, monkeypatch, kind=kind)
    for _ in range(20):
        assert grid.get("/api/session", headers=ADMIN_TOKEN).status_code == 200
        assert oidc.problem() is None
    assert endpoint.requests == []


# ---- a sign-in with a managed identity ---------------------------------------------------------------


@pytest.mark.parametrize("identity", ["user", "system"])
@pytest.mark.parametrize("kind", KINDS)
def test_a_sign_in_with_a_managed_identity(grid, db, idp, endpoint, monkeypatch, kind, identity):
    provider = managed(idp, endpoint, monkeypatch, kind=kind, identity=identity)
    requests = token_requests(monkeypatch)
    assert oidc.problem() is None and oidc.client_method() == "federated"
    r = through(grid, provider)
    assert signin_code(r) is None and cookie_set(r, "grid-session") and count(db, "userSessions") == 1
    expected = {"api-version": API_VERSIONS[kind], "resource": AUDIENCE}
    if identity == "user":
        expected["client_id"] = MI
    assert [asked["query"] for asked in endpoint.requests] == [expected, expected]  # the start, and the redemption
    for asked in endpoint.requests:
        assert asked["header_ok"] and not asked["forwarded"]
        assert asked["path"] == (fake_idp.IMDS_PATH if kind == "imds" else fake_idp.APP_SERVICE_PATH)
        assert ("Metadata" if kind == "imds" else "X-Identity-Header") in asked["headers"]
    ((url, form, headers),) = requests
    assert url == provider.base_url + "/token" and "Authorization" not in headers and "client_secret" not in form
    assert form["client_assertion"] == endpoint.issued[-1]
    assert form["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    (assertion,) = provider.assertions
    assert assertion["raw"] == endpoint.issued[-1]
    assert assertion["claims"]["sub"] == (fake_idp.OBJECT_ID_MI if identity == "user" else SYSTEM_OBJECT_ID)
    assert assertion["claims"]["iss"] == fake_idp.TENANT_ISSUER and assertion["claims"]["aud"] == AUDIENCE


@pytest.mark.parametrize("audience", ["api://AzureADTokenExchange", "api://AzureADTokenExchangeUSGov",
                                      "api://AzureADTokenExchangeChina"])
def test_the_audience_follows_the_cloud(grid, db, idp, endpoint, monkeypatch, audience):
    provider = managed(idp, endpoint, monkeypatch)
    monkeypatch.setitem(oidc.ENTRA_AUDIENCES, "127.0.0.1", audience)
    provider.trust(endpoint.tenant, subject=fake_idp.OBJECT_ID_MI, audience=audience)
    assert signin_code(through(grid, provider)) is None
    assert {asked["query"]["resource"] for asked in endpoint.requests} == {audience}
    assert provider.assertions[-1]["claims"]["aud"] == audience
    assert told(grid, headers=ADMIN_TOKEN)["clientToken"]["credential"]["audience"] == audience


def test_the_token_is_asked_for_at_each_use(grid, db, idp, endpoint, monkeypatch):
    """Two sign-ins and the Admin page within a minute: the first start and both redemptions ask (three); with the
    memo off, everything asks (five). The token is kept nowhere, the memo included."""
    provider = managed(idp, endpoint, monkeypatch)
    for _ in range(2):
        assert signin_code(through(grid, provider)) is None
    assert told(grid, headers=ADMIN_TOKEN)["clientToken"]["decoded"] is True
    assert len(endpoint.requests) == 3
    sent = [a["raw"] for a in provider.assertions]
    assert len(sent) == 2 and sent[0] != sent[1] and set(sent) <= set(endpoint.issued)
    del endpoint.requests[:]
    monkeypatch.setattr(oidc, "IDENTITY_MEMO_S", 0)
    for _ in range(2):
        assert signin_code(through(grid, provider)) is None
    told(grid, headers=ADMIN_TOKEN)
    assert len(endpoint.requests) == 5
    needles = [endpoint.header]
    for token in endpoint.issued:
        needles += [token] + token.split(".")[1:]  # the header part is the tenant's, the same in every token
    assert _held({"kept": [endpoint.issued[0].split(".")[2]]}, needles, set())
    assert oidc._identity_memo is not None and set(oidc._identity_memo.claims) <= set(oidc._SHOWN_CLAIMS)
    assert [name for name, value in vars(oidc).items() if _held(value, needles, set())] == []


@pytest.mark.parametrize("kind", KINDS)
def test_the_query_is_ours_alone(grid, db, idp, endpoint, monkeypatch, kind):
    managed(idp, endpoint, monkeypatch, kind=kind)
    oidc.check_client()
    (asked,) = endpoint.requests
    assert sorted(asked["query"]) == ["api-version", "client_id", "resource"]
    if kind == "app-service":
        monkeypatch.setenv("IDENTITY_ENDPOINT", endpoint.app_service_url + "?object_id=x")
        assert oidc.problem() == CANT.format(WHERE_NOT_LOCAL.format(endpoint.app_service_url))
        oidc.reset_caches()
        assert signin_code(start(grid)) == "misconfigured" and len(endpoint.requests) == 1


# ---- the memo ------------------------------------------------------------------------------------


def test_a_dead_endpoint_holds_no_worker_long(grid, db, idp, endpoint, monkeypatch):
    managed(idp, endpoint, monkeypatch)
    monkeypatch.setattr(oidc, "IDENTITY_TIMEOUT_S", 0.5)
    endpoint.fault = "slow"
    oidc.discovery()  # the provider is not what is timed
    began = time.monotonic()
    codes = [signin_code(start(grid)) for _ in range(10)]
    took = time.monotonic() - began
    assert codes == ["unavailable"] * 10 and len(endpoint.requests) == 1
    assert took < 0.5 + 9 * 0.2, took


@pytest.mark.parametrize("fault, code", [("status_403", "misconfigured"), ("status_503", "unavailable")])
def test_a_failure_is_remembered_for_a_minute(grid, db, idp, endpoint, monkeypatch, caplog, fault, code):
    """Remembered with whether asking again may help: a sign-in under way is turned away, at its callback, the way
    a start would be."""
    provider = managed(idp, endpoint, monkeypatch)
    clock = stopped_clock(monkeypatch)
    back = provider.approve(start(grid).headers["Location"])  # started while all was well
    assert len(endpoint.requests) == 1
    clock[0] += oidc.IDENTITY_MEMO_S
    endpoint.fault = fault
    caplog.set_level(logging.WARNING, logger="ui.app")
    assert signin_code(start(grid)) == code and len(endpoint.requests) == 2
    sentence = app_warnings(caplog)[-1][len(START):]
    assert sentence.startswith(CANT.format("")[:-1])
    assert "HTTP {}, ".format(fault[7:]) in sentence or "HTTP {}. ".format(fault[7:]) in sentence  # no error code
    endpoint.fault = None
    clock[0] += oidc.IDENTITY_MEMO_S - 1
    assert signin_code(start(grid)) == code and len(endpoint.requests) == 2
    assert signin_code(grid.get(local(back))) == code and len(endpoint.requests) == 2
    assert app_warnings(caplog)[-2:] == [START + sentence, REDEEM + sentence]
    assert provider.counts["token"] == 0
    assert told(grid, headers=ADMIN_TOKEN)["problem"] == sentence and len(endpoint.requests) == 2
    clock[0] += 1
    r = start(grid)
    assert r.status_code == 303 and r.headers["Location"].startswith(provider.base_url + "/authorize?")
    assert len(endpoint.requests) == 3


def test_a_success_is_remembered_but_the_token_is_not(grid, db, idp, endpoint, monkeypatch):
    provider = managed(idp, endpoint, monkeypatch)
    browsers = [grid, grid_app.app.test_client()]  # two people: each sign-in keeps its own login cookie
    backs = [provider.approve(start(browser).headers["Location"]) for browser in browsers]
    assert len(endpoint.requests) == 1
    assert signin_code(browsers[0].get(local(backs[0]))) is None and len(endpoint.requests) == 2
    assert signin_code(browsers[1].get(local(backs[1]))) is None and len(endpoint.requests) == 3
    shown = [told(grid, headers=ADMIN_TOKEN)["clientToken"] for _ in range(2)]
    assert shown[0] == shown[1] and shown[0]["subject"] == fake_idp.OBJECT_ID_MI and len(endpoint.requests) == 3


def test_the_memo_is_for_these_settings(grid, db, idp, endpoint, monkeypatch):
    provider = managed(idp, endpoint, monkeypatch)
    endpoint.assigned[OTHER_CLIENT_ID] = OTHER_OBJECT_ID
    provider.trust(endpoint.tenant, subject=OTHER_OBJECT_ID)
    start(grid)
    assert [asked["query"]["client_id"] for asked in endpoint.requests] == [MI]
    monkeypatch.setenv("GRID_OIDC_MANAGED_IDENTITY", OTHER_CLIENT_ID)
    start(grid)
    assert [asked["query"]["client_id"] for asked in endpoint.requests] == [MI, OTHER_CLIENT_ID]
    assert told(grid, headers=ADMIN_TOKEN)["clientToken"]["subject"] == OTHER_OBJECT_ID
    assert len(endpoint.requests) == 2


# ---- the token endpoint of a managed identity (X2) ------------------------------------------------------


def x2(host="127.0.0.1"):
    return ("the discovery document's token_endpoint isn't https on {}, GRID_OIDC_ISSUER's host, and a managed "
            "identity's token is sent nowhere else".format(host))


@pytest.mark.parametrize("allow_http", [None, "1"])
@pytest.mark.parametrize("elsewhere", ["non-loopback", "localhost", "another address", "another port"])
def test_a_managed_identity_goes_only_to_the_issuers_host(grid, db, idp, endpoint, monkeypatch, caplog, elsewhere,
                                                          allow_http):
    provider = managed(idp, endpoint, monkeypatch)
    port = provider.server.server_port
    address = {"non-loopback": "http://{}:{}/token".format(non_loopback_address(), port),
               "localhost": "http://localhost:{}/token".format(port),
               "another address": "https://127.0.0.2:{}/token".format(port),
               "another port": "http://127.0.0.1:{}/token".format(endpoint.server.server_port)}[elsewhere]
    if allow_http:
        monkeypatch.setenv("GRID_OIDC_ALLOW_HTTP", "1")
    caplog.set_level(logging.WARNING, logger="ui.app")
    back = provider.approve(start(grid).headers["Location"])  # started with the provider's own token endpoint
    asked = len(endpoint.requests)
    provider.token_endpoint = address
    oidc.reset_caches()
    # Without the setting, an http address that isn't loopback is refused with the whole document, before X2.
    says = x2() if allow_http or elsewhere != "non-loopback" else "the discovery document's token_endpoint isn't an https URL"
    assert signin_code(grid.get(local(back))) == "misconfigured"
    r = start(grid)
    assert signin_code(r) == "misconfigured" and cookie_set(r, "grid-login") is None
    assert app_warnings(caplog) == [REDEEM + says, START + says]
    assert len(endpoint.requests) == asked  # checked before the endpoint is asked
    assert told(grid, headers=ADMIN_TOKEN)["problem"] == says[:1].upper() + says[1:] + "."
    assert provider.counts["token"] == 0 and provider.assertions == []
    assert oidc.plain_http_warning() is None  # the token can't go there, so nothing is warned


@pytest.mark.parametrize("allow_http", [None, "1"])
def test_a_managed_identity_goes_only_over_https(monkeypatch, allow_http):
    """The issuer's own host and port, over plain http: refused for its scheme alone, whatever GRID_OIDC_ALLOW_HTTP
    says. The same address over https is taken."""
    configure(monkeypatch, GRID_OIDC_ISSUER=ENTRA, GRID_OIDC_CLIENT_SECRET=None, GRID_OIDC_CLIENT_AUTH="federated",
              GRID_OIDC_MANAGED_IDENTITY=MI, GRID_OIDC_ALLOW_HTTP=allow_http, **{OURS: None, THEIRS: None})
    path = "/{}/oauth2/v2.0/token".format(fake_idp.TENANT_ID)
    for refused in ("http://login.microsoftonline.com" + path, "HTTP://login.microsoftonline.com" + path):
        assert oidc._split(refused).hostname == "login.microsoftonline.com" and oidc._split(refused).port is None
        with pytest.raises(oidc.Misconfigured) as e:
            oidc._client_auth_method({"token_endpoint": refused, "token_endpoint_auth_methods_supported": ALL_METHODS},
                                     oidc.settings())
        assert str(e.value) == x2("login.microsoftonline.com"), refused
    for taken in ("https://login.microsoftonline.com" + path, "https://LOGIN.microsoftonline.com" + path):
        assert oidc._client_auth_method({"token_endpoint": taken, "token_endpoint_auth_methods_supported": ALL_METHODS},
                                        oidc.settings()) == "federated", taken


# ---- what goes wrong at the endpoint -----------------------------------------------------------------


def _expected(case, kind, endpoint, identity):
    """(the sentence, whether it is transient) for a case of test_what_goes_wrong_at_the_endpoint."""
    at = where(endpoint, kind)
    named = ("its system-assigned managed identity turned on" if identity == "system" else
             "the user-assigned managed identity with client id {} assigned to it".format(identity))
    refused = "{} refused to give a token: HTTP {}. Check that this VM or app has " + named
    busy = ("{} answered HTTP {}, which Azure says it does while it updates or is busy: try again in a minute, and "
            "if it keeps happening, check that the identity is turned on and assigned")
    def status(number, error):
        """As the status reads: App Service's error answer has no error code (statusCode and message only)."""
        return "{} {}".format(number, error) if kind == "imds" else str(number)

    found = {
        "slow": ("{} didn't answer within 0.5 s".format(at), True),
        "stopped": ("{} refused the connection".format(at), True),
        "hang_up": ("{} can't be reached".format(at), True),
        "garbage": ("{} can't be reached".format(at), True),
        "redirect": ("{} answered with a redirect (HTTP 302), which isn't followed".format(at), False),
        "html": ("{} didn't answer with a JSON object".format(at), False),
        "not_json": ("{} didn't answer with a JSON object".format(at), False),
        "json_list": ("{} didn't answer with a JSON object".format(at), False),
        "oversize": ("{} answered more than 65536 bytes".format(at), False),
        "expired": ("the token {} gave has already expired; check this machine's clock".format(at), False),
        "unknown client id": (refused.format(at, status(400, "invalid_request")), False),
        "system without one": (refused.format(at, status(400, "invalid_request")), False),
    }
    for fault in ("no_token", "not_a_jwt", "huge_token"):
        found[fault] = ("{} answered without a token (no access_token of three base64url parts, at most 16384 "
                        "bytes)".format(at), False)
    for number, error in fake_idp._STATUS_ERRORS.items():
        name = "status_{}".format(number)
        if number in (404, 410, 429) or number >= 500:
            found[name] = (busy.format(at, status(number, error)), True)
        else:
            found[name] = (refused.format(at, status(number, error)), False)
    sentence, transient = found[case]
    return CANT.format(sentence), transient


ENDPOINT_CASES = list(fake_idp.IDENTITY_FAULTS) + ["stopped", "unknown client id", "system without one"]


def _go_wrong(case, endpoint, monkeypatch):
    """Switches the case on. Returns the identity the settings name."""
    if case in fake_idp.IDENTITY_FAULTS:
        endpoint.fault = case
    elif case == "stopped":
        endpoint.stop()
    elif case == "unknown client id":
        monkeypatch.setenv("GRID_OIDC_MANAGED_IDENTITY", OTHER_CLIENT_ID)
        return OTHER_CLIENT_ID
    else:
        monkeypatch.setenv("GRID_OIDC_MANAGED_IDENTITY", "system")
        endpoint.system = None
        return "system"
    return MI


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", ENDPOINT_CASES)
def test_what_goes_wrong_at_the_endpoint(grid, db, idp, endpoint, monkeypatch, caplog, kind, case):
    """Each by its sentence: between a start and its callback, and at a start; the person sees unavailable (try
    again in a minute) or misconfigured, nobody is signed in, nothing reaches the provider's token endpoint, and the
    Admin page says why."""
    provider = managed(idp, endpoint, monkeypatch, kind=kind)
    monkeypatch.setattr(oidc, "IDENTITY_TIMEOUT_S", 0.5)
    caplog.set_level(logging.WARNING, logger="ui.app")
    back = provider.approve(start(grid).headers["Location"])
    identity = _go_wrong(case, endpoint, monkeypatch)
    sentence, transient = _expected(case, kind, endpoint, identity)
    code = "unavailable" if transient else "misconfigured"
    r = grid.get(local(back))
    assert signin_code(r) == code and cookie_set(r, "grid-session") is None
    assert app_warnings(caplog) == [REDEEM + sentence]
    oidc.reset_caches()  # the memo too: the start asks again
    pending = logins(db)
    r = start(grid)
    assert signin_code(r) == code and cookie_set(r, "grid-login") is None and logins(db) == pending
    assert app_warnings(caplog) == [REDEEM + sentence, START + sentence]
    body = told(grid, headers=ADMIN_TOKEN)
    assert body["problem"] == sentence and body["clientToken"]["problem"] == sentence
    assert body["clientToken"]["decoded"] is (case == "expired")
    assert provider.counts["token"] == 0 and count(db, "userSessions") == 0
    assert fake_idp.DESCRIPTION_MARKER not in caplog.text and endpoint.header not in caplog.text


def test_a_redirect_is_never_followed(grid, db, idp, endpoint, monkeypatch):
    managed(idp, endpoint, monkeypatch)
    endpoint.fault = "redirect"
    assert signin_code(start(grid)) == "misconfigured"
    assert len(endpoint.requests) == 1 and endpoint.elsewhere == 0


def test_an_endpoint_that_hangs(idp, endpoint, monkeypatch):
    managed(idp, endpoint, monkeypatch)
    monkeypatch.setattr(oidc, "IDENTITY_TIMEOUT_S", 0.5)
    endpoint.fault, endpoint.delay_s = "slow", 1.5
    began = time.monotonic()
    token, _, sentence, transient = oidc._managed_identity_token(oidc.settings())
    assert time.monotonic() - began < 1.4
    assert (token, transient) == (None, True)
    assert sentence == CANT.format(where(endpoint, "app-service") + " didn't answer within 0.5 s")


def test_a_request_urllib_wont_make(idp, endpoint, monkeypatch):
    """Not expected, as the address and the header are checked first: a sentence of our own, without the
    exception's words, and not one that asking again would help."""
    managed(idp, endpoint, monkeypatch)

    class Refusing:
        def open(self, *args, **kwargs):
            raise ValueError("words-of-the-exception")

    monkeypatch.setattr(oidc.urllib.request, "build_opener", lambda *handlers: Refusing())
    token, _, sentence, transient = oidc._managed_identity_token(oidc.settings())
    assert (token, transient) == (None, False) and endpoint.requests == []
    assert sentence == CANT.format(where(endpoint, "app-service") + " couldn't be asked: urllib refused to make the "
                                                                    "request")


def test_the_answer_is_bounded(grid, db, idp, endpoint, monkeypatch):
    provider = managed(idp, endpoint, monkeypatch)
    endpoint.answer_size = 65536
    token, _, sentence, _ = oidc._managed_identity_token(oidc.settings())
    assert token == endpoint.issued[-1] and sentence is None
    endpoint.answer_size = 65537
    assert oidc._managed_identity_token(oidc.settings())[2].endswith("answered more than 65536 bytes.")
    endpoint.answer_size = None
    endpoint.token_size = 16384
    oidc.reset_caches()
    assert signin_code(through(grid, provider)) is None
    assert len(provider.assertions[-1]["raw"]) == 16384 and provider.assertions[-1]["raw"] == endpoint.issued[-1]
    endpoint.token_size = 16385
    assert oidc._managed_identity_token(oidc.settings())[2].endswith("at most 16384 bytes).")


def test_no_proxy_sees_the_endpoint(grid, db, idp, endpoint, monkeypatch):
    provider = managed(idp, endpoint, monkeypatch)
    recorder = fake_idp.serve_recorder()
    try:
        proxied_through(recorder, monkeypatch, endpoint.app_service_url)
        for kind in KINDS:
            point_at(endpoint, kind, monkeypatch)
            oidc.reset_caches()
            assert signin_code(through(grid, provider)) is None
        assert recorder.count == 1 and len(endpoint.requests) == 4
    finally:
        recorder.stop()


def test_an_expired_managed_identity_token_is_never_sent(grid, db, idp, endpoint, monkeypatch):
    provider = managed(idp, endpoint, monkeypatch)
    endpoint.fault = "expired"
    assert signin_code(start(grid)) == "misconfigured" and provider.counts["token"] == 0
    # One within the clock skew is sent, and a provider whose clock is behind takes it.
    endpoint.fault, endpoint.lifetime_s = None, -30
    provider.clock_offset_s = -60
    monkeypatch.setenv("GRID_OIDC_CLOCK_SKEW_S", "120")
    oidc.reset_caches()
    assert signin_code(through(grid, provider)) is None
    assert provider.assertions[-1]["raw"] == endpoint.issued[-1]
    assert claims_of(endpoint.issued[-1])["exp"] < time.time()


# ---- what the Admin page says to add ---------------------------------------------------------------


def test_the_credential_shown_is_microsofts_not_the_tokens(grid, db, idp, endpoint, monkeypatch, tmp_path):
    provider = managed(idp, endpoint, monkeypatch)
    credential = {"issuer": provider.base_url, "subject": fake_idp.OBJECT_ID_MI, "audience": AUDIENCE}
    token = told(grid, headers=ADMIN_TOKEN)["clientToken"]
    assert token["credential"] == credential
    assert (token["issuer"], token["subject"], token["audience"]) == (fake_idp.TENANT_ISSUER, fake_idp.OBJECT_ID_MI,
                                                                      [AUDIENCE])
    endpoint.aud_form, endpoint.iss_form = "guid", "v1"
    oidc.reset_caches()
    token = told(grid, headers=ADMIN_TOKEN)["clientToken"]
    assert token["credential"] == credential
    assert token["audience"] == [fake_idp.RESOURCE_APP_ID]
    assert token["issuer"] == "https://sts.windows.net/{}/".format(fake_idp.TENANT_ID)
    # A token file: the token's own lines are what the credential must name.
    monkeypatch.delenv("GRID_OIDC_MANAGED_IDENTITY")
    write_token(tmp_path / "token", fake_idp.FakeCluster())
    monkeypatch.setenv(OURS, str(tmp_path / "token"))
    assert told(grid, headers=ADMIN_TOKEN)["clientToken"]["credential"] is None
    # The hints name the credential's values, and the tab's rows by the labels it shows.
    rows = ("Credential issuer", "Credential subject", "Credential audience")
    page = (pathlib.Path(oidc.__file__).parent / "static" / "js" / "views" / "admin" / "signin.js").read_text("utf-8")
    assert all("<dt>{}</dt>".format(row) in page for row in rows)
    for hint in (oidc._ENTRA_HINTS[70021], oidc._ENTRA_HINTS[70025], oidc._METHOD_HINTS["managed-identity"]):
        assert "GRID_OIDC_ISSUER" in hint and "(api://AzureADTokenExchange in the public cloud)" in hint, hint
        assert all(row in hint for row in rows) and "Federated credential" not in hint, hint
    assert "api://AzureADTokenExchange" in oidc._ENTRA_HINTS[700212]
    assert "whatever audience the token itself states" in oidc._ENTRA_HINTS[700212]


def test_a_refused_managed_identity_is_hinted_by_the_credential(grid, db, idp, endpoint, monkeypatch, tmp_path,
                                                                caplog):
    """invalid_client with no number known here: for a managed identity the hint names the credential's values,
    never the token's own issuer and audience, which may be written differently; for a token file, the token's."""
    provider = managed(idp, endpoint, monkeypatch)
    del provider.federated[:]
    provider.trust(fake_idp.FakeCluster(fake_idp.TENANT_ISSUER), subject=fake_idp.OBJECT_ID_MI)  # another key
    caplog.set_level(logging.WARNING, logger="ui.app")
    assert oidc.hint_method() == "managed-identity"
    assert signin_code(through(grid, provider)) == "misconfigured"
    assert app_warnings(caplog) == [REFUSED + "HTTP 401 invalid_client. " + MANAGED_IDENTITY_HINT]
    monkeypatch.delenv("GRID_OIDC_MANAGED_IDENTITY")
    write_token(tmp_path / "token", fake_idp.FakeCluster())
    monkeypatch.setenv(OURS, str(tmp_path / "token"))
    assert oidc.hint_method() == "federated"


# ---- messages ------------------------------------------------------------------------------------


def test_no_message_speaks_only_of_aks():
    """AKS is named only where something is AKS's own: the identity binding's audience. (signin.js and users.js
    are held to the same by the page's tests.)"""
    hint = oidc._ENTRA_HINTS[700212]
    assert inspect.getsource(oidc).count("AKS") == hint.count("AKS") == 2
    assert "AKS" not in inspect.getsource(grid_app)
    for number in (700211, 700213):
        hint = oidc._ENTRA_HINTS[number]
        assert "managed identity" in hint and ("token file" in hint or "Kubernetes" in hint), number
    assert "system:serviceaccount" in oidc._ENTRA_HINTS[700213] and "principal" in oidc._ENTRA_HINTS[700213]


# ---- nothing secret ------------------------------------------------------------------------------


def test_nothing_secret_with_a_managed_identity(grid, db, idp, endpoint, monkeypatch, caplog):
    """The managed identity's tokens (whole and each part), IDENTITY_HEADER's value, and every word of the endpoint's
    and the provider's own: in no log line at any level, no answer and no audit row, through both endpoints and
    every way they go wrong. Only the listed lines are logged."""
    caplog.set_level(logging.DEBUG)
    provider = managed(idp, endpoint, monkeypatch)
    monkeypatch.setenv("GRID_OIDC_ADMINS", "sub:" + fake_idp.USER["sub"])
    monkeypatch.setattr(oidc, "IDENTITY_TIMEOUT_S", 0.5)
    responses, codes, hidden = [], [], {endpoint.header, fake_idp.DESCRIPTION_MARKER, fake_idp.TRACE_ID,
                                        fake_idp.CORRELATION_ID, "a-refused-header-value"}
    answers, fetch = [], oidc._fetch

    def recorded(url, **kwargs):
        document = fetch(url, **kwargs)
        answers.append(document)
        return document

    monkeypatch.setattr(oidc, "_fetch", recorded)

    def asked():
        return [grid.get(url, headers=ADMIN_TOKEN) for url in ("/api/admin/sign-in", "/api/session", "/api/admin/audit")]

    def attempt(between=None, before=None):
        """One sign-in: the endpoint's fault before is on from its start, between from after its start."""
        grid_app.reset_login_starts()
        oidc.reset_caches()
        endpoint.fault = before
        try:
            r = start(grid)
            responses.append(r)
            if not r.headers["Location"].startswith(provider.base_url):
                codes.append(signin_code(r))
                return
            hidden.add(run(db, "SELECT codeVerifier FROM oidcLogins ORDER BY id DESC LIMIT 1")[0][0])
            back = provider.approve(r.headers["Location"])
            hidden.add(code_of(back))
            endpoint.fault = between or before
            r = grid.get(local(back))
            responses.append(r)
            codes.append(signin_code(r))
            responses.extend(asked())
            if cookie_set(r, "grid-session"):
                responses.append(grid.post("/api/session/signout", json={}))
        finally:
            endpoint.fault = None

    for kind in KINDS:
        point_at(endpoint, kind, monkeypatch)
        attempt()
        for fault in fake_idp.IDENTITY_FAULTS:
            attempt(between=fault)
            attempt(before=fault)
            responses.extend(asked())
    # Entra-style refusals: a credential for another object id, for another issuer, and another key's token.
    point_at(endpoint, "app-service", monkeypatch)
    for issuer, subject in ((endpoint.tenant, OTHER_OBJECT_ID),
                            (fake_idp.FakeCluster("https://login.microsoftonline.com/other/v2.0"), fake_idp.OBJECT_ID_MI),
                            (fake_idp.FakeCluster(fake_idp.TENANT_ISSUER), fake_idp.OBJECT_ID_MI)):
        del provider.federated[:]
        provider.trust(issuer, subject=subject)
        attempt()
    del provider.federated[:]
    provider.trust(endpoint.tenant, subject=fake_idp.OBJECT_ID_MI)
    # X2, a header of the wrong value, and one that can't be sent.
    provider.token_endpoint = "http://localhost:{}/token".format(provider.server.server_port)
    attempt()
    provider.token_endpoint = None
    monkeypatch.setenv("IDENTITY_HEADER", "a-refused-header-value")
    attempt()
    monkeypatch.setenv("IDENTITY_HEADER", "a-refused-header-value with a space")
    attempt()
    responses.extend(asked())
    for document in answers:
        hidden.update(document[k] for k in ("id_token", "access_token", "refresh_token") if k in document)
    for token in endpoint.issued:
        hidden.update([token] + [part for part in token.split(".")[1:] if len(part) > 12])
    assert codes.count(None) == 2 and {"misconfigured", "unavailable"} <= set(codes)
    assert len(endpoint.issued) > 20 and len(responses) > 100
    logged = "".join("{}\n{}\n".format(record.getMessage(), record.exc_text or "") for record in caplog.records)
    logged += caplog.text
    assert "error codes 700213)" in logged and "error codes 700211)" in logged
    rows = "\n".join(repr(row) for row in run(db, "SELECT * FROM auditLog"))
    answered = "".join(shown_in(r) for r in responses)
    assert fake_idp.OBJECT_ID_MI in answered  # the claims an admin is shown
    for value in hidden:
        assert value and value not in logged, "logged"
        assert value not in answered, "answered"
        assert value not in rows, "kept in the audit log"
    met = listed_lines_only(caplog)
    assert {("ui.oidc", 0), ("ui.app", 0), ("ui.app", 1), ("ui.app", 2)} <= met


# ---- the Admin page's answer ---------------------------------------------------------------------


@pytest.mark.parametrize("identity", ["user", "system"])
@pytest.mark.parametrize("kind", KINDS)
def test_what_the_admin_page_is_told_with_a_managed_identity(grid, db, idp, endpoint, monkeypatch, kind, identity):
    provider = managed(idp, endpoint, monkeypatch, kind=kind, identity=identity)
    subject = fake_idp.OBJECT_ID_MI if identity == "user" else SYSTEM_OBJECT_ID
    address = endpoint.imds_url if kind == "imds" else endpoint.app_service_url

    def expected(token, problem=None):
        return {"configured": True, "problem": problem, "warning": None, "label": "single sign-on",
                "issuer": provider.base_url, "clientId": "grid-ui", "clientAuth": "federated",
                "clientMethod": "federated", "clientToken": token, "platformTokenUnused": False,
                "redirectUri": CALLBACK, "scopes": ["openid", "email", "profile"], "groupsClaims": ["groups"],
                "admins": [], "session": {"hours": 12, "idleMinutes": 120}, "readsOpen": False, "adminToken": True,
                "endSession": False}

    def facts(**changes):
        exp = claims_of(endpoint.issued[-1])["exp"]
        return dict({"source": "managed-identity", "setting": "GRID_OIDC_MANAGED_IDENTITY", "file": None,
                     "identity": MI if identity == "user" else "system", "host": kind, "endpoint": address,
                     "decoded": True, "issuer": fake_idp.TENANT_ISSUER, "subject": subject, "audience": [AUDIENCE],
                     "expiresAt": store._iso(oidc._utc(exp)), "expired": False,
                     "credential": {"issuer": provider.base_url, "subject": subject, "audience": AUDIENCE},
                     "problem": None}, **changes)

    assert told(grid, headers=ADMIN_TOKEN) == expected(facts())
    assert len(endpoint.requests) == 1
    # The endpoint refuses (asked when the memo is stale): its sentence is the page's problem.
    endpoint.fault = "status_403"
    monkeypatch.setattr(oidc, "_identity_memo", None)
    sentence = CANT.format("{} refused to give a token: HTTP 403{}. Check that this VM or app has {}".format(
        where(endpoint, kind), " forbidden" if kind == "imds" else "",  # App Service's answer has no error code
        "the user-assigned managed identity with client id {} assigned to it".format(MI)
        if identity == "user" else "its system-assigned managed identity turned on"))
    nothing = {"decoded": False, "issuer": None, "subject": None, "audience": [], "expiresAt": None}
    refused = expected(facts(credential={"issuer": provider.base_url, "subject": None, "audience": AUDIENCE},
                             problem=sentence, **nothing), problem=sentence)
    assert told(grid, headers=ADMIN_TOKEN) == refused and len(endpoint.requests) == 2
    assert told(grid, headers=ADMIN_TOKEN) == refused and len(endpoint.requests) == 2  # within the minute
    # A problem with the environment: nothing asked, no endpoint shown.
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    if kind == "app-service":
        monkeypatch.setenv("IDENTITY_ENDPOINT", endpoint.app_service_url + "?x=1")
    p11 = oidc.problem()
    assert p11.startswith("GRID_OIDC_MANAGED_IDENTITY can't be used: ")
    assert told(grid, headers=ADMIN_TOKEN) == expected(
        facts(host=None, endpoint=None, credential={"issuer": provider.base_url, "subject": None, "audience": AUDIENCE},
              **nothing), problem=p11)
    assert len(endpoint.requests) == 2
    # Who may read it: as for a token file.
    client = grid_app.app.test_client()
    assert client.get("/api/admin/sign-in").status_code == 403
    sign_in_as(client, make_user(db, "Vic Viewer", [permissions.VIEWER]))
    r = client.get("/api/admin/sign-in")
    assert r.status_code == 403 and subject not in r.get_data(as_text=True)
    sign_in_as(client, make_user(db, "Rita Reader", [add_role(db, "Readers", "users.view")]))
    assert told(client)["clientToken"]["identity"] == (MI if identity == "user" else "system")
    for answer in (client.get("/api/admin/sign-in"), grid.get("/api/admin/sign-in", headers=ADMIN_TOKEN)):
        assert endpoint.header not in answer.get_data(as_text=True)
