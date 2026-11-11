"""Signing in with OpenID Connect: oidc.py and the flow against a fake identity provider.
Run from the repo root: python -m pytest ui/tests"""
import datetime
import json
import logging
import re
import time
import types
import urllib.parse

import pytest

from ui import accounts, app as grid_app, auth, oidc, permissions, store
from ui.tests import fake_idp
from ui.tests.test_accounts import (add_item, add_role, add_session, db, hashed, make_user, run, sign_in_as,  # noqa: F401
                                    ts)  # (db is a fixture)

CALLBACK = "http://localhost/auth/callback"
# A configuration with nothing wrong in it.
GOOD = {
    "GRID_OIDC_ISSUER": "https://login.example.com",
    "GRID_OIDC_CLIENT_ID": fake_idp.CLIENT_ID,
    "GRID_OIDC_CLIENT_SECRET": fake_idp.CLIENT_SECRET,
    "GRID_UI_PUBLIC_URL": "https://grid.example.com/",
}
SETTINGS = ("GRID_OIDC_ISSUER", "GRID_OIDC_CLIENT_ID", "GRID_OIDC_CLIENT_SECRET", "GRID_OIDC_CLIENT_AUTH",
            "GRID_OIDC_FEDERATED_TOKEN_FILE", "AZURE_FEDERATED_TOKEN_FILE", "GRID_OIDC_MANAGED_IDENTITY", "GRID_UI_PUBLIC_URL", "GRID_OIDC_LABEL", "GRID_OIDC_SCOPES", "GRID_OIDC_GROUPS_CLAIM", "GRID_OIDC_ADMINS",
            "GRID_OIDC_END_SESSION", "GRID_OIDC_CA_FILE", "GRID_OIDC_ALLOW_HTTP", "GRID_OIDC_CLOCK_SKEW_S",
            "GRID_SESSION_HOURS", "GRID_SESSION_IDLE_MINUTES")
SUBJECT = fake_idp.USER["sub"]
# A discovery document for GOOD's issuer that the Grid UI can use.
DOCUMENT = {
    "issuer": "https://login.example.com",
    "authorization_endpoint": "https://login.example.com/authorize",
    "token_endpoint": "https://login.example.com/token",
    "jwks_uri": "https://login.example.com/jwks",
    "response_types_supported": ["code", "code id_token"],
    "code_challenge_methods_supported": ["plain", "S256"],
    "id_token_signing_alg_values_supported": ["HS256", "RS256"],
}


def configure(monkeypatch, **changes):
    """GOOD with changes (None: unset), and no other single sign-on setting."""
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    for name, value in dict(GOOD, **changes).items():
        if value is not None:
            monkeypatch.setenv(name, value)


def stopped_clock(monkeypatch):
    """oidc's monotonic clock, as a list to move on by hand (the real one keeps telling the time)."""
    clock = [1000.0]
    monkeypatch.setattr(oidc, "time", types.SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    return clock


@pytest.fixture
def idp(monkeypatch):
    """A fake identity provider at 127.0.0.1, which the settings point at; the grid is at http://localhost/."""
    provider = fake_idp.serve("127.0.0.1")
    provider.redirect_uri = CALLBACK
    configure(monkeypatch, GRID_OIDC_ISSUER=provider.base_url, GRID_UI_PUBLIC_URL="http://localhost/")
    yield provider
    provider.stop()


def code_of(callback):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(callback).query)["code"][0]


def sign_in(provider):
    """(ID token, nonce) of a sign-in through the provider as its user, as the callback gets them."""
    login = oidc.new_login()
    return oidc.redeem(code_of(provider.approve(oidc.authorize_url(login))), login.verifier), login.nonce


def refusal(call):
    with pytest.raises(oidc.SignInFailed) as failed:
        call()
    return failed.value


# ---- oidc.py ----------------------------------------------------------------------------


def test_settings_and_their_defaults(monkeypatch):
    configure(monkeypatch)
    s = oidc.settings()
    assert (s.issuer, s.client_id, s.client_secret) == ("https://login.example.com", "grid-ui", "s3cret-client")
    assert (s.public_url, s.public_path, s.public_host) == ("https://grid.example.com/", "/", "grid.example.com")
    assert (s.client_auth, s.label, s.scopes, s.groups_claims, s.admins) == ("auto", "single sign-on", (), ("groups",), ())
    assert (s.end_session, s.ca_file, s.allow_http) == (False, "", False)
    assert (s.skew, s.session_hours, s.idle_minutes) == (60, 12, 120)
    assert oidc.configured() and oidc.problem() is None
    assert oidc.redirect_uri() == "https://grid.example.com/auth/callback"
    monkeypatch.delenv("GRID_OIDC_ISSUER")
    assert not oidc.configured() and oidc.problem() is None


def test_lists_switches_and_numbers_in_the_settings(monkeypatch):
    configure(monkeypatch, GRID_OIDC_SCOPES=" groups  offline_access openid groups ", GRID_OIDC_LABEL="Contoso",
              GRID_OIDC_GROUPS_CLAIM=" groups, roles ,,groups", GRID_OIDC_ADMINS="sub:00u-dana, Ops@Example.com ,",
              GRID_OIDC_CLIENT_AUTH=" POST ", GRID_OIDC_END_SESSION="1", GRID_OIDC_ALLOW_HTTP="1",
              GRID_OIDC_CLOCK_SKEW_S="0", GRID_SESSION_HOURS="168", GRID_SESSION_IDLE_MINUTES="0")
    s = oidc.settings()
    assert (s.scopes, s.label, s.groups_claims) == (("groups", "offline_access"), "Contoso", ("groups", "roles"))
    assert (s.admins, s.client_auth, s.end_session, s.allow_http) == (("sub:00u-dana", "Ops@Example.com"), "post", True, True)
    assert (s.skew, s.session_hours, s.idle_minutes) == (0, 168, 0)
    # Out of range reads as the default (and problem() says so).
    configure(monkeypatch, GRID_OIDC_CLOCK_SKEW_S="301", GRID_SESSION_HOURS="a day", GRID_SESSION_IDLE_MINUTES="4")
    s = oidc.settings()
    assert (s.skew, s.session_hours, s.idle_minutes) == (60, 12, 120)


@pytest.mark.parametrize("given, url, path, host", [
    ("https://grid.example.com", "https://grid.example.com/", "/", "grid.example.com"),
    ("https://Grid.Example.com:443/ui", "https://Grid.Example.com:443/ui/", "/ui/", "grid.example.com"),
    ("http://127.0.0.1:4444/", "http://127.0.0.1:4444/", "/", "127.0.0.1:4444"),
    ("http://[::1]:8080", "http://[::1]:8080/", "/", "[::1]:8080"),
    (None, "", "/", ""),
])
def test_the_public_url_gets_a_trailing_slash(monkeypatch, given, url, path, host):
    configure(monkeypatch, GRID_UI_PUBLIC_URL=given)
    s = oidc.settings()
    assert (s.public_url, s.public_path, s.public_host) == (url, path, host)


@pytest.mark.parametrize("netloc, host", [
    ("grid.example.com", "grid.example.com"),
    ("GRID.example.com:443", "grid.example.com"),
    ("grid.example.com:80", "grid.example.com"),
    ("grid.example.com:8443", "grid.example.com:8443"),
    ("127.0.0.1:4444", "127.0.0.1:4444"),
    ("[::1]:4444", "[::1]:4444"),
    ("[::1]", "[::1]"),
    ("grid.example.com:port", ""),
    ("[::1", ""),
])
def test_hosts_are_compared_in_one_form(netloc, host):
    assert oidc.normal_host(netloc) == host


@pytest.mark.parametrize("public_url, names", [
    ("https://grid.example.com/", ("__Host-grid-session", "__Host-grid-login", "/", True, True)),
    ("https://grid.example.com/grid", ("grid-session", "grid-login", "/grid/", True, False)),
    ("http://localhost:4444/", ("grid-session", "grid-login", "/", False, False)),
])
def test_cookie_names_follow_the_public_url(monkeypatch, public_url, names):
    """__Host- needs https and the path /."""
    configure(monkeypatch, GRID_UI_PUBLIC_URL=public_url)
    assert tuple(oidc.cookie_names()) == names


CLIENT_ID = "Set GRID_OIDC_CLIENT_ID to the Grid UI's client id at the identity provider."
NO_SECRET = ("Set GRID_OIDC_CLIENT_SECRET, or GRID_OIDC_CLIENT_AUTH=federated to use a token the platform issues, so the "
             "Grid UI can prove to the identity provider that it is the client.")
METHODS = "GRID_OIDC_CLIENT_AUTH is auto, basic, post or federated."
PUBLIC = "Set GRID_UI_PUBLIC_URL to the address people open the Grid UI at, e.g. https://grid.example.com/."
HTTPS = "{} must be an https URL (or set GRID_OIDC_ALLOW_HTTP=1 for development)."
TENANT = ("GRID_OIDC_ISSUER is a multi-tenant address (common, organizations or consumers), which no ID token states "
          "as its issuer. Use your tenant's: https://login.microsoftonline.com/<tenant id>/v2.0.")
ADMINS = "GRID_OIDC_ADMINS entry {!r} is neither an email address nor sub:<subject>."


@pytest.mark.parametrize("changes, problem", [
    ({"GRID_OIDC_CLIENT_SECRET": None}, NO_SECRET),
    ({"GRID_OIDC_CLIENT_ID": None, "GRID_UI_PUBLIC_URL": None}, CLIENT_ID),  # the first problem, not every one
    ({"GRID_UI_PUBLIC_URL": None}, PUBLIC),
    ({"GRID_UI_PUBLIC_URL": "grid.example.com"}, PUBLIC),
    ({"GRID_UI_PUBLIC_URL": "ftp://grid.example.com/"}, PUBLIC),
    ({"GRID_OIDC_ISSUER": "http://login.example.com"}, HTTPS.format("GRID_OIDC_ISSUER")),
    ({"GRID_OIDC_ISSUER": "login.example.com"}, HTTPS.format("GRID_OIDC_ISSUER")),
    ({"GRID_UI_PUBLIC_URL": "http://grid.example.com/"}, HTTPS.format("GRID_UI_PUBLIC_URL")),
    ({"GRID_OIDC_ISSUER": "http://login.example.com", "GRID_UI_PUBLIC_URL": "http://grid.example.com/",
      "GRID_OIDC_ALLOW_HTTP": "1"}, None),
    ({"GRID_OIDC_ISSUER": "http://127.0.0.1:8080/realms/grid", "GRID_UI_PUBLIC_URL": "http://localhost:4444/"}, None),
    ({"GRID_OIDC_ISSUER": "https://login.microsoftonline.com/common/v2.0"}, TENANT),
    ({"GRID_OIDC_ISSUER": "https://login.microsoftonline.com/Organizations/v2.0"}, TENANT),
    ({"GRID_OIDC_ISSUER": "https://login.microsoftonline.com/consumers/"}, TENANT),
    ({"GRID_OIDC_ISSUER": "https://login.microsoftonline.com/0b6f7cd2-8a3c-4c35-a1e2-8d0e5b7f19aa/v2.0"}, None),
    ({"GRID_OIDC_CLIENT_AUTH": "private_key_jwt"}, METHODS),
    ({"GRID_OIDC_CLIENT_AUTH": "Basic"}, None),
    ({"GRID_OIDC_ADMINS": "ops@example.com, dana"}, ADMINS.format("dana")),
    ({"GRID_OIDC_ADMINS": "sub:"}, ADMINS.format("sub:")),
    ({"GRID_OIDC_ADMINS": "sub:00u-dana, Ops@Example.com"}, None),
    ({"GRID_OIDC_CLOCK_SKEW_S": "301"}, "GRID_OIDC_CLOCK_SKEW_S is a number of seconds from 0 to 300."),
    ({"GRID_OIDC_CLOCK_SKEW_S": "a minute"}, "GRID_OIDC_CLOCK_SKEW_S is a number of seconds from 0 to 300."),
    ({"GRID_SESSION_HOURS": "0"}, "GRID_SESSION_HOURS is a number of hours from 1 to 168."),
    ({"GRID_SESSION_IDLE_MINUTES": "4"},
     "GRID_SESSION_IDLE_MINUTES is a number of minutes from 5 to 1440, or 0 for never."),
    ({"GRID_SESSION_IDLE_MINUTES": "0"}, None),
])
def test_what_is_wrong_with_the_settings(monkeypatch, changes, problem):
    configure(monkeypatch, **changes)
    assert oidc.problem() == problem
    assert oidc.configured()  # a problem never opens the grid up


def write_certificate(path):
    """A self-signed CA certificate, as PEM."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    signer = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test identity provider CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(signer.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now)
                   .not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .sign(signer, hashes.SHA256()))
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def test_the_extra_ca_file_must_hold_certificates(monkeypatch, tmp_path):
    missing, junk, good = tmp_path / "missing.pem", tmp_path / "junk.pem", tmp_path / "ca.pem"
    junk.write_text("not a certificate\n")
    write_certificate(good)
    configure(monkeypatch, GRID_OIDC_CA_FILE=str(missing))
    assert oidc.problem() == "GRID_OIDC_CA_FILE can't be read: there is no file {}.".format(missing)
    configure(monkeypatch, GRID_OIDC_CA_FILE=str(junk))
    assert oidc.problem() == "GRID_OIDC_CA_FILE can't be read: {} holds no PEM certificates.".format(junk)
    configure(monkeypatch, GRID_OIDC_CA_FILE=str(good))
    assert oidc.problem() is None and oidc.settings().ca_file == str(good)


def test_each_problem_is_logged_once(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="ui.oidc")
    configure(monkeypatch, GRID_OIDC_CLIENT_AUTH="jwt")
    for _ in range(3):
        oidc.problem()
    configure(monkeypatch, GRID_OIDC_ISSUER="http://login.example.com", GRID_UI_PUBLIC_URL="http://grid.example.com/",
              GRID_OIDC_ALLOW_HTTP="1")
    for _ in range(3):
        assert oidc.problem() is None
    assert [r.getMessage() for r in caplog.records] == [
        METHODS,
        "GRID_OIDC_ALLOW_HTTP=1: single sign-on runs over plain http, which is for development only."]


def test_discovery_is_kept_for_an_hour(idp, monkeypatch):
    clock = stopped_clock(monkeypatch)
    first = oidc.discovery()
    assert first["issuer"] == idp.base_url and oidc.discovery() is first and idp.counts["discovery"] == 1
    clock[0] += oidc.CACHE_S
    oidc.discovery()
    assert idp.counts["discovery"] == 2
    oidc.discovery(refresh=True)
    assert idp.counts["discovery"] == 3


def test_a_stale_discovery_document_stands_in_for_a_day(idp, monkeypatch, caplog):
    clock = stopped_clock(monkeypatch)
    first = oidc.discovery()
    idp.fault = "discovery_down"
    clock[0] += oidc.CACHE_S
    with caplog.at_level(logging.WARNING, logger="ui.oidc"):
        assert oidc.discovery() == first
    assert idp.counts["discovery"] == 2
    assert "couldn't refresh the identity provider's discovery document, so the copy from 60 minutes ago" in caplog.text
    assert oidc.discovery() == first and idp.counts["discovery"] == 2  # not asked again for a minute
    clock[0] += oidc.RETRY_S
    assert oidc.discovery() == first and idp.counts["discovery"] == 3
    clock[0] = 1000.0 + oidc.STALE_S
    with pytest.raises(oidc.HttpRefused) as refused:
        oidc.discovery()
    assert refused.value.status == 503
    idp.fault = None
    assert oidc.discovery() == first


def test_an_issuer_ending_in_a_slash(monkeypatch):
    """OpenID Connect Discovery §4: the slash goes before .well-known is added (Auth0's issuers end in one), and
    the issuer must still match with it."""
    provider = fake_idp.serve("127.0.0.1", path="/tenant/")
    try:
        provider.redirect_uri = CALLBACK
        configure(monkeypatch, GRID_OIDC_ISSUER=provider.base_url, GRID_UI_PUBLIC_URL="http://localhost/")
        assert provider.base_url.endswith(":{}/tenant/".format(provider.server.server_port))
        assert oidc.discovery()["issuer"] == provider.base_url and provider.counts["discovery"] == 1
        assert oidc.verify_id_token(*sign_in(provider))["iss"] == provider.base_url
        monkeypatch.setenv("GRID_OIDC_ISSUER", provider.base_url.rstrip("/"))
        with pytest.raises(oidc.Misconfigured):
            oidc.discovery()
    finally:
        provider.stop()


def test_discovery_must_state_the_issuer_byte_for_byte(idp, monkeypatch):
    idp.fault = "discovery_issuer_mismatch"
    with pytest.raises(oidc.Misconfigured) as wrong:
        oidc.discovery()
    assert "GRID_OIDC_ISSUER" in str(wrong.value)
    idp.fault = None
    monkeypatch.setenv("GRID_OIDC_ISSUER", idp.base_url + "/")
    with pytest.raises(oidc.Misconfigured):
        oidc.discovery()


@pytest.mark.parametrize("fault, error", [
    ("discovery_down", oidc.HttpRefused),
    ("discovery_redirect", oidc.Unavailable),
    ("html_content_type", oidc.Unavailable),
])
def test_discovery_that_fails(idp, fault, error):
    idp.fault = fault
    with pytest.raises(error):
        oidc.discovery()
    assert sum(idp.counts.values()) == 1  # a redirect is never followed


@pytest.mark.parametrize("change, complaint", [
    ({"issuer": "https://login.example.com/"}, "the discovery document's issuer is 'https://login.example.com/'"),
    ({"token_endpoint": "http://login.example.com/token"}, "token_endpoint isn't an https URL"),
    ({"authorization_endpoint": "javascript:alert(1)"}, "authorization_endpoint isn't an https URL"),
    ({"jwks_uri": None}, "jwks_uri isn't an https URL"),
    ({"response_types_supported": ["id_token"]}, "(response type code)"),
    ({"code_challenge_methods_supported": ["plain"]}, "S256"),
    ({"id_token_signing_alg_values_supported": ["HS256", "none"]}, "signs ID tokens with none of RS256"),
    ({"id_token_signing_alg_values_supported": "RS256"}, "signs ID tokens with none of RS256"),
])
def test_discovery_documents_that_do_not_fit(monkeypatch, change, complaint):
    configure(monkeypatch)
    oidc._check_discovery(DOCUMENT, oidc.settings())
    oidc._check_discovery({k: v for k, v in DOCUMENT.items() if not k.endswith("_supported")}, oidc.settings())
    with pytest.raises(oidc.Misconfigured) as wrong:
        oidc._check_discovery(dict(DOCUMENT, **change), oidc.settings())
    assert complaint in str(wrong.value)


@pytest.mark.parametrize("url", [
    "http://login.example.com/jwks", "file:///etc/passwd", "ftp://login.example.com/jwks", "//login.example.com/jwks",
    "jwks", "", None,
])
def test_the_back_channel_fetches_only_https(monkeypatch, url):
    """Plain http only to a loopback host (or with GRID_OIDC_ALLOW_HTTP=1), and never another scheme."""
    configure(monkeypatch)
    with pytest.raises(oidc.Unavailable):
        oidc._fetch(url)


def test_json_is_json_whatever_its_parameters(idp):
    """Google serves its key set as application/json; charset=UTF-8. text/html is refused."""
    assert [k["kid"] for k in oidc._fetch(idp.base_url + "/jwks")["keys"]] == ["r1", "e1"]
    idp.fault = "html_content_type"
    with pytest.raises(oidc.Unavailable) as refused:
        oidc.discovery()
    assert "text/html" in str(refused.value)


def test_a_key_set_past_the_size_limit_is_refused(idp):
    signed, nonce = sign_in(idp)
    idp.fault = "jwks_oversize"
    with pytest.raises(oidc.Unavailable) as refused:
        oidc.verify_id_token(signed, nonce)
    assert "more than 262144 bytes" in str(refused.value)


def test_a_sign_in_through_the_provider(idp):
    signed, nonce = sign_in(idp)
    claims = oidc.verify_id_token(signed, nonce)
    assert (claims["iss"], claims["aud"], claims["sub"], claims["nonce"]) == (idp.base_url, "grid-ui", SUBJECT, nonce)
    assert oidc.identity(claims) == {"subject": SUBJECT, "email": "dana@example.com", "emailVerified": True,
                                     "name": "Dana Lee", "groups": ["grid-testers"], "groupsOverage": False}
    assert idp.auth_used == ["client_secret_basic"]
    assert (idp.counts["discovery"], idp.counts["jwks"], idp.counts["token"]) == (1, 1, 1)


def test_an_es256_signature(idp):
    idp.alg = "ES256"
    assert oidc.verify_id_token(*sign_in(idp))["sub"] == SUBJECT


@pytest.mark.parametrize("fault, status, error", [
    ("token_400_invalid_grant", 400, "invalid_grant"),
    ("token_401_invalid_client", 401, "invalid_client"),
    ("token_401_entra_70021", 401, "invalid_client"),
    ("token_500", 500, "server_error"),
])
def test_the_token_endpoint_refusing(idp, fault, status, error):
    idp.fault = fault
    with pytest.raises(oidc.HttpRefused) as refused:
        sign_in(idp)
    assert (refused.value.status, refused.value.error) == (status, error)
    assert refused.value.codes == ((70021,) if fault == "token_401_entra_70021" else ())


def test_a_token_endpoint_that_does_not_answer_in_time(idp, monkeypatch):
    monkeypatch.setattr(oidc, "TOKEN_TIMEOUT_S", 0.5)
    idp.fault, idp.delay_s = "token_slow", 1.5
    started = time.monotonic()
    with pytest.raises(oidc.Unavailable):
        sign_in(idp)
    assert time.monotonic() - started < 1.4


def test_a_code_is_redeemed_once_and_with_its_verifier(idp):
    login = oidc.new_login()
    code = code_of(idp.approve(oidc.authorize_url(login)))
    with pytest.raises(oidc.HttpRefused) as refused:
        oidc.redeem(code, oidc.new_login().verifier)
    assert (refused.value.status, refused.value.error) == (400, "invalid_grant")
    with pytest.raises(oidc.HttpRefused) as refused:
        oidc.redeem(code, login.verifier)  # the right verifier, but the code was used up
    assert refused.value.error == "invalid_grant"
    login = oidc.new_login()
    code = code_of(idp.approve(oidc.authorize_url(login)))
    oidc.redeem(code, login.verifier)
    with pytest.raises(oidc.HttpRefused):
        oidc.redeem(code, login.verifier)


def test_only_the_id_token_is_taken_from_the_token_endpoint(idp, monkeypatch):
    signed, _ = sign_in(idp)
    assert isinstance(signed, str) and signed.count(".") == 2
    monkeypatch.setattr(oidc, "_fetch", lambda url, **kwargs: {"access_token": "at-x", "token_type": "Bearer"})
    failed = refusal(lambda: oidc.redeem("a-code", "a-verifier"))  # discovery is still held
    assert failed.reason == "no_id_token"


@pytest.mark.parametrize("setting, listed, used", [
    ("auto", ["client_secret_basic", "client_secret_post"], "client_secret_basic"),
    ("auto", ["client_secret_post"], "client_secret_post"),
    ("auto", None, "client_secret_basic"),  # not listed: the standard's default
    ("basic", ["client_secret_basic"], "client_secret_basic"),
    ("post", ["client_secret_post"], "client_secret_post"),
    ("post", ["client_secret_basic", "client_secret_post"], "client_secret_post"),
])
def test_client_authentication(idp, monkeypatch, setting, listed, used):
    monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", setting)
    idp.auth_methods = listed
    assert oidc.verify_id_token(*sign_in(idp))["sub"] == SUBJECT
    assert idp.auth_used == [used]


def test_a_client_registered_for_one_method(idp, monkeypatch):
    """An Okta app set to client_secret_post refuses Basic with invalid_client; GRID_OIDC_CLIENT_AUTH says which."""
    idp.auth_methods = ["client_secret_post"]
    monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", "basic")
    with pytest.raises(oidc.HttpRefused) as refused:
        sign_in(idp)
    assert (refused.value.status, refused.value.error) == (401, "invalid_client")


def test_a_client_secret_with_awkward_characters(idp, monkeypatch):
    """RFC 6749 §2.3.1: Basic authentication encodes each part first, so a ':' or a '%' survives."""
    idp.client_secret = "p:a%2Fss w+rd/é~"
    monkeypatch.setenv("GRID_OIDC_CLIENT_SECRET", idp.client_secret)
    assert oidc.verify_id_token(*sign_in(idp))["sub"] == SUBJECT
    monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", "post")
    assert oidc.verify_id_token(*sign_in(idp))["sub"] == SUBJECT
    assert idp.auth_used == ["client_secret_basic", "client_secret_post"]


# The provider's faults that make an ID token the Grid UI refuses: (fault, the reason it gives, the subject it
# keeps for the audit log).
REFUSED_ID_TOKENS = [
    ("wrong_iss", "issuer", None),
    ("wrong_aud", "audience", None),
    ("multi_aud_no_azp", "azp", SUBJECT),
    ("azp_other", "azp", SUBJECT),
    ("expired", "expired", SUBJECT),
    ("iat_future", "issued_in_future", SUBJECT),
    ("nbf_future", "not_yet_valid", SUBJECT),
    ("bad_signature", "signature", None),
    ("alg_none", "algorithm", None),
    ("hs256_public_key", "algorithm", None),
    ("unknown_kid", "unknown_key", None),
    ("kid_missing_many_keys", "unknown_key", None),
    ("crit_header", "crit", None),
    ("no_nonce", "nonce", SUBJECT),
    ("wrong_nonce", "nonce", SUBJECT),
    ("missing_sub", "missing_claim", None),
    ("long_sub", "subject", None),
    ("surrogate_sub", "subject", None),
    ("oversize_id_token", "malformed", None),
]


@pytest.mark.parametrize("fault, reason, subject", REFUSED_ID_TOKENS)
def test_id_tokens_that_are_refused(idp, fault, reason, subject):
    """The subject is kept, for the audit log, only once the token was read and its sub is usable."""
    idp.fault = fault
    signed, nonce = sign_in(idp)
    failed = refusal(lambda: oidc.verify_id_token(signed, nonce))
    assert (failed.reason, failed.subject) == (reason, subject)


@pytest.mark.parametrize("fault, says", [
    ("expired", r"the ID token's exp was 60\d s ago by this server's clock"),
    ("iat_future", r"the ID token's iat is (59\d|600) s ahead of this server's clock"),
    ("nbf_future", r"the ID token's nbf is (59\d|600) s ahead of this server's clock"),
])
def test_a_refused_time_says_how_far_off_it_is(idp, fault, says):
    idp.fault = fault
    signed, nonce = sign_in(idp)
    assert re.fullmatch(says, str(refusal(lambda: oidc.verify_id_token(signed, nonce))))


def test_token_times_get_the_clock_skew_both_ways(idp, monkeypatch):
    signed, nonce = sign_in(idp)  # exp is 300 s after iat
    later, earlier = time.time() + 330, time.time() - 30
    for now in (later, earlier):
        assert oidc.verify_id_token(signed, nonce, now=now)["sub"] == SUBJECT
    monkeypatch.setenv("GRID_OIDC_CLOCK_SKEW_S", "0")
    assert refusal(lambda: oidc.verify_id_token(signed, nonce, now=later)).reason == "expired"
    assert refusal(lambda: oidc.verify_id_token(signed, nonce, now=earlier)).reason == "issued_in_future"


def test_an_old_iat_is_no_reason_to_refuse(idp):
    """No lower bound on iat: the provider's clock may be behind the Grid UI's."""
    idp.clock_offset_s = -250  # exp is still 50 s ahead
    assert oidc.verify_id_token(*sign_in(idp))["sub"] == SUBJECT


def test_the_algorithm_must_be_one_the_provider_lists(idp):
    idp.algs, idp.alg = ["RS256"], "ES256"
    signed, nonce = sign_in(idp)
    assert refusal(lambda: oidc.verify_id_token(signed, nonce)).reason == "algorithm"


def test_a_provider_that_lists_no_algorithms_uses_rs256(idp):
    idp.algs = None
    assert oidc.verify_id_token(*sign_in(idp))["sub"] == SUBJECT
    idp.alg = "ES256"
    signed, nonce = sign_in(idp)
    assert refusal(lambda: oidc.verify_id_token(signed, nonce)).reason == "algorithm"


def test_the_key_must_fit_the_algorithm(idp):
    claims = idp.claims("n0nce")
    idp.extra_keys = [dict(idp.jwk("r1"), kid="r9", alg="RS512")]
    for signed in (idp.sign(claims, alg="ES256", kid="r1"),  # an EC signature naming an RSA key
                   idp.sign(claims, alg="RS256", signer="r1", kid="r9")):  # a key published for RS512
        assert refusal(lambda: oidc.verify_id_token(signed, "n0nce")).reason == "key_mismatch"


def test_keys_for_another_use_are_ignored(idp):
    idp.extra_keys = [dict(idp.jwk("r1"), kid="x1", use="enc")]
    signed = idp.sign(idp.claims("n0nce"), kid="x1")
    assert refusal(lambda: oidc.verify_id_token(signed, "n0nce")).reason == "unknown_key"


def test_without_a_kid_the_one_key_that_fits_is_used(idp):
    assert oidc.verify_id_token(idp.sign(idp.claims("n0nce"), kid=False), "n0nce")["sub"] == SUBJECT


def test_keys_are_fetched_once_and_again_for_a_key_nobody_has_seen(idp, monkeypatch):
    clock = stopped_clock(monkeypatch)
    for _ in range(2):
        oidc.verify_id_token(*sign_in(idp))
    assert idp.counts["jwks"] == 1
    idp.rotate()  # signs with r2, which the held key set doesn't have
    oidc.verify_id_token(*sign_in(idp))
    assert idp.counts["jwks"] == 2
    idp.fault = "unknown_kid"
    for _ in range(3):
        signed, nonce = sign_in(idp)
        assert refusal(lambda: oidc.verify_id_token(signed, nonce)).reason == "unknown_key"
    assert idp.counts["jwks"] == 2  # within a minute of the last refetch: the set already held
    clock[0] += oidc.KEY_REFETCH_S
    signed, nonce = sign_in(idp)
    assert refusal(lambda: oidc.verify_id_token(signed, nonce)).reason == "unknown_key"
    assert idp.counts["jwks"] == 3


@pytest.mark.parametrize("signed, reason", [
    (None, "malformed"), (7, "malformed"), ("", "malformed"), ("nodots", "malformed"), ("a.b", "malformed"),
    ("a.b.c.d", "malformed"), ("!!.!!.!!", "malformed"), ("x" * (64 * 1024 - 2) + ".." + "x", "malformed"),
    ("e30.e30.", "algorithm"),  # a header of {}, so no alg
])
def test_what_is_not_a_signed_jwt(idp, signed, reason):
    assert refusal(lambda: oidc.verify_id_token(signed, "n0nce")).reason == reason


def test_an_empty_nonce_matches_nothing(idp):
    idp.fault = "no_nonce"
    signed, _ = sign_in(idp)
    assert refusal(lambda: oidc.verify_id_token(signed, "")).reason == "nonce"


def test_a_nonce_holding_a_lone_surrogate_is_just_another_nonce(idp):
    """JSON can carry a lone surrogate, which no encoding takes as it is: it must not stop the comparison."""
    signed = idp.sign(dict(idp.claims("n0nce"), nonce="n0nce\ud800"))
    assert refusal(lambda: oidc.verify_id_token(signed, "n0nce")).reason == "nonce"


@pytest.mark.parametrize("groups, expected", [
    (["grid-testers", "grid-ops", "grid-testers"], ["grid-testers", "grid-ops"]),
    ("grid-testers", ["grid-testers"]),
    (None, []),  # no groups claim at all
    ([1, None, "", "ok", {"x": 1}, "ok"], ["ok"]),
    ({"grid-testers": True}, []),
    (["lone \ud800", "tab\there", "ok"], ["ok"]),  # no group mapping could name these
])
def test_groups_as_a_list_a_string_or_absent(monkeypatch, groups, expected):
    configure(monkeypatch)
    claims = {"sub": "x"} if groups is None else {"sub": "x", "groups": groups}
    assert oidc.identity(claims)["groups"] == expected


def test_groups_from_several_claims_make_one_list(monkeypatch):
    """GRID_OIDC_GROUPS_CLAIM=groups,roles: Entra's app roles count along with its groups."""
    configure(monkeypatch, GRID_OIDC_GROUPS_CLAIM="groups,roles")
    claims = {"sub": "x", "groups": ["a", "b"], "roles": ["Grid.Admin", "a"], "wids": ["c"]}
    assert oidc.identity(claims)["groups"] == ["a", "b", "Grid.Admin"]


def test_at_most_500_groups_of_255_characters(monkeypatch):
    configure(monkeypatch)
    groups = ["g{:03}-".format(i) + "é" * 250 for i in range(600)]
    found = oidc.identity({"sub": "x", "groups": ["y" * 256] + groups})["groups"]
    assert found == groups[:500] and {len(g) for g in found} == {255}


def test_groups_the_provider_left_out_count_as_none(monkeypatch, caplog):
    """Entra's overage: past 200 groups it names where to fetch them instead (_claim_names)."""
    configure(monkeypatch)
    claims = {"sub": "x", "groups": ["stale"], "_claim_names": {"groups": "src1"},
              "_claim_sources": {"src1": {"endpoint": "https://graph.example.com/groups"}}}
    with caplog.at_level(logging.WARNING, logger="ui.oidc"):
        for _ in range(2):
            found = oidc.identity(claims)
    assert (found["groups"], found["groupsOverage"]) == ([], True)
    assert sum("left the groups out of the ID token" in r.getMessage() for r in caplog.records) == 1
    # Only the overage of a claim that counts.
    assert oidc.identity(dict(claims, _claim_names={"roles": "src1"}))["groups"] == ["stale"]


@pytest.mark.parametrize("claims, name", [
    ({"name": "Dana Lee", "preferred_username": "dana", "email": "dana@example.com"}, "Dana Lee"),
    ({"name": " \t ", "preferred_username": "dana", "email": "dana@example.com"}, "dana"),
    ({"preferred_username": 7, "email": "dana@example.com"}, "dana@example.com"),
    ({"name": "Dana\x00 Lee\x1b[31m"}, "Dana Lee[31m"),
    ({"name": "Dana \ud800Lee"}, "Dana Lee"),  # a lone surrogate: JSON can carry one, no database can store it
    ({"name": "D" * 300}, "D" * 255),
    ({"email": "dana\n@example.com"}, None),
    ({"email": "dana\ud800@example.com"}, None),
    ({}, None),
])
def test_the_name_to_show(monkeypatch, claims, name):
    configure(monkeypatch)
    assert oidc.identity(dict(claims, sub="x"))["name"] == name


@pytest.mark.parametrize("email, verified, expected", [
    ("dana@example.com", True, ("dana@example.com", True)),
    ("dana@example.com", "true", ("dana@example.com", True)),
    ("dana@example.com", 1, ("dana@example.com", False)),
    ("dana@example.com", "True", ("dana@example.com", False)),
    ("dana@example.com", None, ("dana@example.com", False)),
    ("d" * 250 + "@x.com", True, (None, True)),
    (["dana@example.com"], True, (None, True)),
    ("dana\ud800@example.com", True, (None, True)),
])
def test_emails_and_whether_they_were_verified(monkeypatch, email, verified, expected):
    configure(monkeypatch)
    found = oidc.identity({"sub": "x", "email": email, "email_verified": verified})
    assert (found["email"], found["emailVerified"]) == expected


def test_admins_by_subject_or_by_verified_email():
    admins = ("sub:00u-Dana", "Ops@Example.com")
    assert oidc.admin_match({"subject": "00u-Dana"}, admins)
    assert not oidc.admin_match({"subject": "00u-dana"}, admins)  # a subject matches exactly
    assert oidc.admin_match({"subject": "x", "email": "ops@example.COM", "emailVerified": True}, admins)
    assert oidc.admin_match({"subject": "x", "email": "ops@example.com", "emailVerified": 1}, admins)  # a users row
    assert not oidc.admin_match({"subject": "x", "email": "ops@example.com", "emailVerified": False}, admins)
    assert not oidc.admin_match({"subject": "Ops@Example.com", "email": None, "emailVerified": True}, admins)
    assert not oidc.admin_match({"subject": "00u-Dana"}, ())


def test_the_pkce_challenge_of_rfc_7636():
    assert oidc.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_a_new_sign_in_gets_fresh_values(monkeypatch):
    configure(monkeypatch)
    one, two = oidc.new_login(), oidc.new_login()
    assert len(one.verifier) == 64 and one.challenge == oidc.pkce_challenge(one.verifier)
    assert len(one.browser) == len(one.state) == len(one.nonce) == 43
    assert len({one.browser, one.state, one.nonce, one.verifier, two.browser, two.state, two.nonce, two.verifier}) == 8


@pytest.mark.parametrize("public_url, given, kept", [
    ("https://grid.example.com/", "A" * 43, True),
    ("https://grid.example.com/", "A" * 44, False),
    ("https://grid.example.com/", "A" * 42 + "!", False),
    ("https://grid.example.com/", None, False),
    ("https://grid.example.com/grid/", "A" * 43, False),  # no __Host-: a sibling subdomain could have set it
    ("http://localhost:4444/", "A" * 43, False),
])
def test_the_browser_value_is_kept_only_from_a_host_cookie(monkeypatch, public_url, given, kept):
    configure(monkeypatch, GRID_UI_PUBLIC_URL=public_url)
    login = oidc.new_login(given)
    assert (login.browser == given) is kept and len(login.browser) == 43


def test_the_authorize_url(idp, monkeypatch):
    """The provider's own query is kept, ours is added, and there is no response_mode."""
    monkeypatch.setenv("GRID_OIDC_SCOPES", "groups offline_access")
    idp.auto = True  # its authorization endpoint ends in ?auto=1
    login = oidc.new_login()
    url = urllib.parse.urlsplit(oidc.authorize_url(login))
    assert url._replace(query="").geturl() == idp.base_url + "/authorize"
    assert urllib.parse.parse_qs(url.query) == {
        "auto": ["1"], "response_type": ["code"], "client_id": ["grid-ui"], "redirect_uri": [CALLBACK],
        "scope": ["openid email profile groups offline_access"], "state": [login.state], "nonce": [login.nonce],
        "code_challenge": [oidc.pkce_challenge(login.verifier)], "code_challenge_method": ["S256"]}


def test_signing_out_of_the_provider_too(idp, monkeypatch):
    assert oidc.end_session_url() is None  # not asked for
    monkeypatch.setenv("GRID_OIDC_END_SESSION", "1")
    url = urllib.parse.urlsplit(oidc.end_session_url())
    assert url._replace(query="").geturl() == idp.base_url + "/logout"
    assert urllib.parse.parse_qs(url.query) == {"client_id": ["grid-ui"], "post_logout_redirect_uri": ["http://localhost/"]}
    oidc.reset_caches()
    idp.fault = "discovery_down"
    assert oidc.end_session_url() is None  # signing out of the grid never waits on the provider


# ---- the flow ---------------------------------------------------------------------------
#
# The Grid UI's routes against the fake provider: Flask's test client is the browser, at http://localhost/ (the
# public URL), and the provider's approve() and deny() stand in for its sign-in page.

ADMIN = "s3cret"  # what the db fixture sets GRID_ADMIN_TOKEN to


@pytest.fixture
def grid(db, idp, monkeypatch):
    """A test client of a Grid UI on the db fixture's sqlite that signs people in with idp."""
    monkeypatch.setattr(grid_app, "PROXY_HOPS", 0)
    return grid_app.app.test_client()


def local(url):
    """A URL's path and query, as the test client asks for it."""
    parts = urllib.parse.urlsplit(url)
    return parts.path + ("?" + parts.query if parts.query else "")


def start(client, return_to="#/mfa", **kwargs):
    """GET /auth/login?return=<return_to>."""
    return client.get("/auth/login?" + urllib.parse.urlencode({"return": return_to}), **kwargs)


def through(client, idp, return_to="#/mfa", deny=False, **kwargs):
    """Start a sign-in and come back from the provider, its user signing in (or with deny, pressing Cancel): the
    callback's response."""
    r = start(client, return_to, **kwargs)
    assert r.status_code == 303 and r.headers["Location"].startswith(idp.base_url + "/authorize?"), r.headers
    back = (idp.deny if deny else idp.approve)(r.headers["Location"])
    return client.get(local(back), **kwargs)


def cookie_set(r, name):
    """The response's Set-Cookie for that cookie as {"value": ..., and each attribute by its lower-case name}, or
    None."""
    for header in r.headers.getlist("Set-Cookie"):
        first, *attributes = [part.strip() for part in header.split(";")]
        key, _, value = first.partition("=")
        if key == name:
            found = {"value": value}
            for attribute in attributes:
                attribute_name, _, attribute_value = attribute.partition("=")
                found[attribute_name.lower()] = attribute_value or True
            return found
    return None


def signin_code(r):
    """The ?signin= code of a redirect back to the page (None: signed in)."""
    assert r.status_code == 303 and r.headers["Location"].startswith("../"), r.headers
    return urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["Location"]).query).get("signin", [None])[0]


def count(path, table):
    return run(path, "SELECT COUNT(*) FROM {}".format(table))[0][0]


def counted_lookups(monkeypatch):
    """The session lookups the gate runs from now on (their tokens), counted as they happen."""
    provider = next(p for p in auth.PROVIDERS if isinstance(p, auth.OidcSessionProvider))
    calls, lookup = [], provider.lookup

    def counted(token):
        calls.append(token)
        return lookup(token)

    monkeypatch.setattr(provider, "lookup", counted)
    return calls


def test_without_single_sign_on_there_is_nothing_to_sign_in_with(db):
    client = grid_app.app.test_client()
    assert client.get("/auth/login?return=%23%2Fmfa").status_code == 404
    assert client.get("/auth/callback?code=c&state=s").status_code == 404


def test_a_sign_in_starts_at_the_provider_with_a_login_cookie(grid, idp, db):
    r = start(grid, "#/mfa")
    assert r.status_code == 303 and r.headers["Cache-Control"] == "no-store"
    url = urllib.parse.urlsplit(r.headers["Location"])
    query = urllib.parse.parse_qs(url.query)
    (browser_hash, state_hash, nonce, verifier, return_to), = run(
        db, "SELECT browserHash, stateHash, nonce, codeVerifier, returnTo FROM oidcLogins")
    assert url._replace(query="").geturl() == idp.base_url + "/authorize"
    assert query == {"response_type": ["code"], "client_id": ["grid-ui"], "redirect_uri": [CALLBACK],
                     "scope": ["openid email profile"], "state": query["state"], "nonce": [nonce],
                     "code_challenge": [oidc.pkce_challenge(verifier)], "code_challenge_method": ["S256"]}
    assert state_hash == hashed(query["state"][0]) and return_to == "#/mfa"
    login = cookie_set(r, "grid-login")
    assert browser_hash == hashed(login["value"])
    assert (login["max-age"], login["path"], login["httponly"], login["samesite"]) == ("600", "/", True, "Lax")
    assert "secure" not in login  # http://localhost/


def test_a_round_trip(grid, idp, db):
    r = through(grid, idp, "#/mfa")
    assert r.headers["Location"] == "../#/mfa" and r.headers["Cache-Control"] == "no-store"
    session = cookie_set(r, "grid-session")
    assert (session["max-age"], session["path"], session["httponly"], session["samesite"]) == (
        "43200", "/", True, "Strict")
    assert "secure" not in session and len(session["value"]) == 43
    assert cookie_set(r, "grid-login")["max-age"] == "0"  # no other tab is signing in
    assert count(db, "oidcLogins") == 0
    (user_id,), = run(db, "SELECT id FROM users")
    assert run(db, "SELECT userId, tokenHash, address FROM userSessions") == [
        (user_id, hashed(session["value"]), "127.0.0.1")]
    assert grid.get("/api/session").get_json() == {
        "signedIn": True, "name": "Dana Lee", "method": "oidc", "adminConfigured": True,
        "providers": [{"id": "token", "kind": "secret", "label": "Admin token"},
                      {"id": "oidc", "kind": "redirect", "label": "single sign-on", "start": "auth/login"}],
        "user": {"id": user_id, "email": "dana@example.com", "emailVerified": True, "subject": SUBJECT,
                 "issuer": idp.base_url},
        "permissions": [], "roles": [], "admin": False, "readsOpen": False, "unavailable": False}


def test_a_sign_in_started_on_one_worker_finishes_on_the_other(grid, idp):
    """Nothing but the database and the browser's login cookie carries a sign-in from its start to its end."""
    r = start(grid)
    back = local(idp.approve(r.headers["Location"]))
    oidc.reset_caches()
    accounts.reset_caches()
    grid_app.reset_login_starts()
    other = grid_app.app.test_client()
    other.set_cookie("grid-login", cookie_set(r, "grid-login")["value"])
    r = other.get(back)
    assert r.headers["Location"] == "../#/mfa" and cookie_set(r, "grid-session")
    assert idp.counts["discovery"] == 2  # the other worker fetched its own


def test_with_an_https_public_url_the_cookies_are_host_cookies(grid, idp, monkeypatch):
    monkeypatch.setenv("GRID_UI_PUBLIC_URL", "https://localhost/")
    idp.redirect_uri = "https://localhost/auth/callback"
    r = start(grid, base_url="https://localhost")
    login = cookie_set(r, "__Host-grid-login")
    assert (login["secure"], login["httponly"], login["samesite"], login["path"], login["max-age"]) == (
        True, True, "Lax", "/", "600")
    r = grid.get(local(idp.approve(r.headers["Location"])), base_url="https://localhost")
    session = cookie_set(r, "__Host-grid-session")
    assert (session["secure"], session["httponly"], session["samesite"], session["path"], session["max-age"]) == (
        True, True, "Strict", "/", "43200")
    assert grid.get("/api/session", base_url="https://localhost").get_json()["name"] == "Dana Lee"


@pytest.mark.parametrize("fault, code", [
    ("discovery_down", "unavailable"),
    ("discovery_redirect", "unavailable"),
    ("html_content_type", "unavailable"),
    ("discovery_issuer_mismatch", "misconfigured"),
])
def test_a_provider_whose_discovery_fails(grid, idp, db, caplog, fault, code):
    idp.fault = fault
    with caplog.at_level(logging.WARNING, logger="ui.app"):
        r = start(grid)
    assert r.status_code == 303 and r.headers["Location"] == "../?signin={}#/mfa".format(code)
    assert cookie_set(r, "grid-login") is None and count(db, "oidcLogins") == 0
    assert "couldn't start a sign-in: " in caplog.text


@pytest.mark.parametrize("fault, code, logged", [
    ("token_400_invalid_grant", "failed", "refused a sign-in's code: HTTP 400 invalid_grant"),
    ("token_401_invalid_client", "misconfigured", "refused a sign-in's code: HTTP 401 invalid_client"),
    ("token_401_entra_70021", "misconfigured", "refused a sign-in's code: HTTP 401 invalid_client (error codes 70021)"),
    ("token_500", "unavailable", "refused a sign-in's code: HTTP 500 server_error"),
    ("token_slow", "unavailable", "couldn't redeem a sign-in's code: the identity provider can't be reached"),
    ("jwks_oversize", "unavailable", "couldn't check a sign-in's ID token: the identity provider answered more than"),
])
def test_a_provider_that_fails_once_the_sign_in_started(grid, idp, db, monkeypatch, caplog, fault, code, logged):
    monkeypatch.setattr(oidc, "TOKEN_TIMEOUT_S", 0.5)
    idp.delay_s = 1.5
    r = start(grid)
    idp.fault = fault
    with caplog.at_level(logging.WARNING, logger="ui.app"):
        r = grid.get(local(idp.approve(r.headers["Location"])))
    assert signin_code(r) == code and r.headers["Location"].endswith("#/mfa")
    assert cookie_set(r, "grid-session") is None and count(db, "userSessions") == 0
    assert logged in caplog.text
    assert count(db, "auditLog") == 0  # no ID token was read, so nobody was refused


@pytest.mark.parametrize("fault, reason, subject", REFUSED_ID_TOKENS)
def test_an_id_token_that_is_refused_signs_nobody_in(grid, idp, db, caplog, fault, reason, subject):
    r = start(grid)
    idp.fault = fault
    with caplog.at_level(logging.WARNING, logger="ui.app"):
        r = grid.get(local(idp.approve(r.headers["Location"])))
    assert signin_code(r) == "failed" and cookie_set(r, "grid-session") is None
    assert count(db, "users") == count(db, "userSessions") == 0
    details = dict({"reason": reason}, **({"subject": subject} if subject else {}))
    assert [(actor, method, address, action, json.loads(d)) for actor, method, address, action, d in run(
        db, "SELECT actorName, actorMethod, address, action, details FROM auditLog")] == [
        (subject or "?", "oidc", "127.0.0.1", "auth.signin_refused", details)]
    assert "refused a sign-in's ID token ({}): ".format(reason) in caplog.text


@pytest.mark.parametrize("fault, says", [
    ("expired", r"exp was 60\d s ago by this server's clock"),
    ("iat_future", r"iat is (59\d|600) s ahead of this server's clock"),
    ("nbf_future", r"nbf is (59\d|600) s ahead of this server's clock"),
])
def test_the_log_says_how_far_off_a_refused_time_is(grid, idp, caplog, fault, says):
    r = start(grid)
    idp.fault = fault
    with caplog.at_level(logging.WARNING, logger="ui.app"):
        assert signin_code(grid.get(local(idp.approve(r.headers["Location"])))) == "failed"
    assert re.search(says, caplog.text)


def test_a_provider_clock_behind_the_database_still_signs_in(grid, idp):
    """No lower bound on iat: here it is earlier than the sign-in's start by the database's clock."""
    idp.clock_offset_s = -250  # exp is still 50 s ahead
    r = through(grid, idp)
    assert r.headers["Location"] == "../#/mfa" and cookie_set(r, "grid-session")


@pytest.mark.parametrize("change", [{"code": None}, {"code": "c" * 2049}, {"iss": "https://impostor.example.com"}])
def test_a_callback_without_a_usable_code_or_from_another_issuer(grid, idp, change):
    r = start(grid)
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(idp.approve(r.headers["Location"])).query))
    for name, value in change.items():
        if value is None:
            del query[name]
        else:
            query[name] = value
    assert signin_code(grid.get("/auth/callback?" + urllib.parse.urlencode(query))) == "failed"
    assert idp.counts["token"] == 0


def test_a_callback_that_names_the_issuer(grid, idp):
    """RFC 9207: the provider may say who it is; it must be the one configured."""
    r = start(grid)
    back = idp.approve(r.headers["Location"]) + "&" + urllib.parse.urlencode({"iss": idp.base_url})
    assert signin_code(grid.get(local(back))) is None


def test_a_callback_from_another_browser_finds_nothing_and_spoils_nothing(grid, idp, db):
    """Login CSRF and stolen callback URLs: a sign-in finishes only in the browser that started it, and another
    browser's try leaves it for that one."""
    r = start(grid)
    back = local(idp.approve(r.headers["Location"]))
    attacker = grid_app.app.test_client()
    assert signin_code(attacker.get(back)) == "expired"  # no login cookie at all
    start(attacker)
    assert signin_code(attacker.get(back)) == "expired"  # a login cookie, but not the one the sign-in is bound to
    assert count(db, "oidcLogins") == 2 and idp.counts["token"] == 0
    r = grid.get(back)
    assert r.headers["Location"] == "../#/mfa" and cookie_set(r, "grid-session")


def test_without_host_cookies_a_planted_login_cookie_is_not_reused(grid, db):
    """A sibling subdomain can set a grid-login cookie (it can't set __Host- ones)."""
    planted = "A" * 43
    grid.set_cookie("grid-login", planted)
    value = cookie_set(start(grid), "grid-login")["value"]
    assert value != planted and run(db, "SELECT browserHash FROM oidcLogins") == [(hashed(value),)]


def test_a_callback_works_once(grid, idp):
    r = start(grid)
    browser = cookie_set(r, "grid-login")["value"]
    back = local(idp.approve(r.headers["Location"]))
    assert cookie_set(grid.get(back), "grid-session")
    grid.set_cookie("grid-login", browser)  # as if the browser had kept it
    r = grid.get(back)
    assert signin_code(r) == "expired" and cookie_set(r, "grid-session") is None


def test_a_sign_in_older_than_ten_minutes_has_expired(grid, idp, db):
    r = start(grid)
    run(db, "UPDATE oidcLogins SET createdAt = ?", (ts(accounts.LOGIN_TTL_S + 1),))
    assert signin_code(grid.get(local(idp.approve(r.headers["Location"])))) == "expired"
    assert idp.counts["token"] == 0


@pytest.mark.parametrize("given, lands", [
    ("#/sessions?node=pw-1.lab.local&status=any", "#/sessions?node=pw-1.lab.local&status=any"),
    ("#/admin?tab=users&user=3", "#/admin?tab=users&user=3"),
    ("https://evil.example.com", "#/overview"),
    ("//evil.example.com", "#/overview"),
    ("/\\evil.example.com", "#/overview"),
    ("javascript:alert(1)", "#/overview"),
    ("#/mfa\r\nLocation: https://evil.example.com", "#/overview"),
    ("#/mfa\n", "#/overview"),
    ("#/" + "a" * 600, "#/overview"),
    ("#mfa", "#/overview"),
    ("", "#/overview"),
])
def test_the_page_s_own_hashes_are_all_a_sign_in_returns_to(grid, idp, db, given, lands):
    r = through(grid, idp, given)
    assert r.headers["Location"] == "../" + lands


def test_cancelling_at_the_provider(grid, idp, db):
    r = through(grid, idp, "#/mfa", deny=True)
    assert r.headers["Location"] == "../?signin=denied#/mfa" and cookie_set(r, "grid-session") is None
    assert "<b>" not in r.get_data(as_text=True)  # the provider's error_description is in no answer
    assert count(db, "oidcLogins") == 0 and idp.counts["token"] == 0  # used up


def test_any_other_error_from_the_provider_is_a_failure(grid, idp):
    r = start(grid)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["Location"]).query)["state"][0]
    r = grid.get("/auth/callback?" + urllib.parse.urlencode({"error": "temporarily_unavailable", "state": state}))
    assert signin_code(r) == "failed"


def test_two_tabs_sign_in_at_once_with_host_cookies(grid, idp, monkeypatch):
    """__Host-grid-login is kept from one start to the next, and expired only once the last tab is back."""
    monkeypatch.setenv("GRID_UI_PUBLIC_URL", "https://localhost/")
    idp.redirect_uri = "https://localhost/auth/callback"
    https = {"base_url": "https://localhost"}
    first, second = start(grid, "#/mfa", **https), start(grid, "#/access", **https)
    assert cookie_set(second, "__Host-grid-login")["value"] == cookie_set(first, "__Host-grid-login")["value"]
    r = grid.get(local(idp.approve(first.headers["Location"])), **https)
    assert r.headers["Location"] == "../#/mfa" and cookie_set(r, "__Host-grid-login") is None
    r = grid.get(local(idp.approve(second.headers["Location"])), **https)
    assert r.headers["Location"] == "../#/access" and cookie_set(r, "__Host-grid-login")["max-age"] == "0"


def test_without_host_cookies_the_second_tab_s_sign_in_wins(grid, idp):
    first, second = start(grid, "#/mfa"), start(grid, "#/access")
    assert cookie_set(second, "grid-login")["value"] != cookie_set(first, "grid-login")["value"]
    assert signin_code(grid.get(local(idp.approve(first.headers["Location"])))) == "expired"
    r = grid.get(local(idp.approve(second.headers["Location"])))
    assert r.headers["Location"] == "../#/access" and cookie_set(r, "grid-session")


def test_at_most_sixty_sign_ins_start_from_one_address_in_ten_minutes(grid, idp, db):
    for _ in range(grid_app.LOGIN_START_LIMIT):
        assert start(grid).headers["Location"].startswith(idp.base_url)
    assert start(grid).headers["Location"] == "../?signin=busy#/mfa"
    assert start(grid, environ_base={"REMOTE_ADDR": "10.0.0.9"}).headers["Location"].startswith(idp.base_url)
    assert count(db, "oidcLogins") == grid_app.LOGIN_START_LIMIT + 1


def test_an_ipv6_address_counts_towards_its_slash_64(grid, idp):
    """One host commonly holds a whole /64: starting from a new address each time doesn't get round the limit."""
    for n in range(grid_app.LOGIN_START_LIMIT):
        r = start(grid, environ_base={"REMOTE_ADDR": "2001:db8:1:2::{:x}".format(n + 1)})
        assert r.headers["Location"].startswith(idp.base_url)
    r = start(grid, environ_base={"REMOTE_ADDR": "2001:db8:1:2:ffff:ffff:ffff:ffff"})
    assert r.headers["Location"] == "../?signin=busy#/mfa"
    assert start(grid, environ_base={"REMOTE_ADDR": "2001:db8:1:3::1"}).headers["Location"].startswith(idp.base_url)


@pytest.mark.parametrize("address, key", [
    ("203.0.113.9", "203.0.113.9"),
    ("::ffff:203.0.113.9", "203.0.113.9"),  # an IPv4 client of a server listening on IPv6
    ("2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2::/64"),
    ("?", "?"),  # no address at all
])
def test_what_sign_in_starts_count_towards(address, key):
    assert grid_app._start_key(address) == key


def test_the_sign_in_starts_counted_never_outgrow_their_bound(monkeypatch):
    """Past the bound, the address that started a sign-in longest ago is forgotten: the table can't grow without
    end, and no start has to look through all of it."""
    monkeypatch.setattr(grid_app, "_START_ADDRESSES_KEPT", 3)
    for n in range(6):
        assert grid_app._start_allowed("10.0.0.{}".format(n))
    assert grid_app._start_allowed("10.0.0.3")  # the oldest of those kept, now the newest
    grid_app._start_allowed("10.0.0.6")
    assert list(grid_app._login_starts) == ["10.0.0.5", "10.0.0.3", "10.0.0.6"]


def test_at_most_max_pending_logins_in_all(grid, idp, monkeypatch, caplog):
    monkeypatch.setattr(accounts, "MAX_PENDING_LOGINS", 3)
    for n in range(3):
        assert start(grid, environ_base={"REMOTE_ADDR": "10.0.0.{}".format(n)}).headers["Location"].startswith(
            idp.base_url)
    with caplog.at_level(logging.WARNING, logger="ui.app"):
        r = start(grid, environ_base={"REMOTE_ADDR": "10.0.0.9"})
    assert r.headers["Location"] == "../?signin=busy#/mfa" and cookie_set(r, "grid-login") is None
    assert "3 sign-ins are on their way through the identity provider already" in caplog.text


def test_a_sign_in_started_at_another_address_moves_to_the_public_one(grid, idp, caplog):
    """The login cookie must be the public address's, where the provider sends the browser back to."""
    elsewhere = {"base_url": "http://127.0.0.1:4444"}
    r = start(grid, "#/mfa", **elsewhere)
    assert r.status_code == 303 and r.headers["Location"] == "http://localhost/auth/login?return=%23%2Fmfa&moved=1"
    assert cookie_set(r, "grid-login") is None
    with caplog.at_level(logging.WARNING, logger="ui.app"):
        for _ in range(2):  # a proxy that rewrites Host brings it back here: go on rather than loop
            r = grid.get("/auth/login?return=%23%2Fmfa&moved=1", **elsewhere)
            assert r.headers["Location"].startswith(idp.base_url + "/authorize?")
    assert caplog.text.count("sign-in was started at 127.0.0.1:4444, but GRID_UI_PUBLIC_URL is http://localhost/; "
                             "have the proxy pass the Host header on") == 1


def test_the_scheme_is_no_reason_to_move(grid, idp, monkeypatch):
    """Behind a proxy that ends TLS every request looks like http: comparing schemes would loop."""
    monkeypatch.setenv("GRID_UI_PUBLIC_URL", "https://localhost/")
    assert start(grid).headers["Location"].startswith(idp.base_url + "/authorize?")


def test_signing_in_again_starts_another_session(grid, idp, db):
    first = cookie_set(through(grid, idp), "grid-session")["value"]
    second = cookie_set(through(grid, idp), "grid-session")["value"]
    assert first != second and count(db, "userSessions") == 2
    other = grid_app.app.test_client()
    other.set_cookie("grid-session", first)  # until it expires, though the browser holds the new one now
    assert other.get("/api/session").get_json()["name"] == "Dana Lee"


@pytest.mark.parametrize("session, disabled, reason", [
    ({"expires_in": -1}, False, "expired"),
    ({"seen_ago": 121 * 60}, False, "idle"),
    ({"revoked_ago": 5}, False, "revoked"),
    ({}, True, "disabled"),
])
def test_a_session_that_ended_asks_for_sign_in_and_drops_its_cookie(grid, db, session, disabled, reason):
    user = make_user(db, "Dana Lee", [permissions.ADMIN], disabled=disabled)
    grid.set_cookie("grid-session", add_session(db, user, **session))
    r = grid.get("/api/tokens")
    assert r.status_code == 403
    assert r.get_json() == {"error": auth.ENDED[reason], "token_required": True, "reason": reason}
    assert cookie_set(r, "grid-session")["max-age"] == "0"


def test_a_cookie_that_is_no_session(grid, db):
    grid.set_cookie("grid-session", "made-up")
    r = grid.get("/api/tokens")
    assert r.status_code == 403 and r.get_json() == {"error": "Sign in to continue.", "token_required": True}
    assert cookie_set(r, "grid-session")["max-age"] == "0"


def test_dead_session_cookies_are_not_guesses(grid, db):
    for _ in range(auth.FAILURE_LIMIT + 1):
        grid.set_cookie("grid-session", "made-up")  # each answer drops it again
        r = grid.get("/api/tokens")
        assert r.status_code == 403 and r.get_json()["token_required"] is True
    for _ in range(auth.FAILURE_LIMIT):
        assert grid.get("/api/tokens", headers={"X-Grid-Token": "wrong"}).status_code == 403
    assert grid.get("/api/tokens", headers={"X-Grid-Token": "wrong"}).status_code == 429


def test_a_wrong_admin_token_counts_even_beside_a_good_session(grid, db):
    """Or anyone signed in could guess the admin token without limit."""
    sign_in_as(grid, make_user(db, "Dana Lee", [permissions.ADMIN]))
    for _ in range(auth.FAILURE_LIMIT):
        assert grid.get("/api/tokens", headers={"X-Grid-Token": "wrong"}).status_code == 200
    assert grid.get("/api/tokens", headers={"X-Grid-Token": "wrong"}).status_code == 429
    assert grid.get("/api/tokens").status_code == 200  # without the guess, the session still works


def test_one_session_lookup_and_one_counted_guess_per_request(grid, db, monkeypatch, tmp_path):
    calls = counted_lookups(monkeypatch)
    sign_in_as(grid, make_user(db, "Dana Lee", [permissions.ADMIN]))
    monkeypatch.setattr(store, "SQLITE_PATH", str(tmp_path))  # a directory: every query fails
    r = grid.get("/api/tokens")
    assert r.status_code == 503 and len(calls) == 1
    assert str(tmp_path) not in r.get_json()["error"]  # the outage hid who asked
    monkeypatch.setattr(store, "SQLITE_PATH", db)

    def broken(*args):
        raise store.DatabaseUnavailable("the database went away at /var/lib/grid.db")

    monkeypatch.setattr(store, "list_tokens", broken)
    r = grid.get("/api/tokens", headers={"X-Grid-Token": "wrong"})
    assert r.status_code == 503 and len(calls) == 2
    assert r.get_json() == {"error": "the database went away at /var/lib/grid.db"}  # Dana is an admin
    assert len(auth._failures["127.0.0.1"]) == 1


def test_a_wrong_admin_token_counts_even_when_the_session_lookup_meets_an_outage(grid, db, monkeypatch, tmp_path):
    """Or a made-up session cookie beside each guess would make guessing free for as long as the database is
    down, with /api/session saying which guess was right."""
    grid.set_cookie("grid-session", "made-up")
    monkeypatch.setattr(store, "SQLITE_PATH", str(tmp_path))  # a directory: every query fails
    for n in range(auth.FAILURE_LIMIT):
        body = grid.get("/api/session", headers={"X-Grid-Token": "wrong-{}".format(n)}).get_json()
        assert (body["signedIn"], body["unavailable"]) == (False, True)
    assert len(auth._failures["127.0.0.1"]) == auth.FAILURE_LIMIT
    assert grid.get("/api/session", headers={"X-Grid-Token": ADMIN}).status_code == 429


def test_the_admin_token_wins_over_a_session_and_needs_no_database(grid, db, monkeypatch, tmp_path):
    calls = counted_lookups(monkeypatch)
    sign_in_as(grid, make_user(db, "Nora Nobody"))
    body = grid.get("/api/session", headers={"X-Grid-Token": ADMIN}).get_json()
    assert (body["name"], body["method"], body["admin"], body["user"]) == ("Admin", "token", True, None)
    assert grid.get("/api/tokens", headers={"X-Grid-Token": ADMIN}).status_code == 200
    assert calls == []
    monkeypatch.setattr(store, "SQLITE_PATH", str(tmp_path))
    body = grid.get("/api/session", headers={"X-Grid-Token": ADMIN}).get_json()
    assert (body["signedIn"], body["method"]) == (True, "token") and calls == []


@pytest.mark.parametrize("changes", [
    {"GRID_OIDC_CLIENT_SECRET": None},
    {"GRID_UI_PUBLIC_URL": "http://grid.example.com/"},
    {"GRID_OIDC_ISSUER": "https://login.microsoftonline.com/common/v2.0"},
    {"GRID_OIDC_CLIENT_AUTH": "private_key_jwt"},
    {"GRID_OIDC_ADMINS": "dana"},
    {"GRID_SESSION_IDLE_MINUTES": "2"},
    {"GRID_OIDC_CLIENT_SECRET": None, "GRID_OIDC_CLIENT_AUTH": "federated",
     "GRID_OIDC_FEDERATED_TOKEN_FILE": "/no/such/token"},
    {"GRID_OIDC_FEDERATED_TOKEN_FILE": "/no/such/token"},
    {"GRID_OIDC_CLIENT_SECRET": None, "AZURE_FEDERATED_TOKEN_FILE": "/no/such/token"},
])
def test_single_sign_on_that_cannot_work_keeps_the_grid_protected(db, monkeypatch, changes):
    configure(monkeypatch, **changes)
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "")
    client = grid_app.app.test_client()
    body = client.get("/api/session").get_json()
    assert body["adminConfigured"] is True and body["permissions"] == []
    assert body["providers"] == [{"id": "oidc", "kind": "redirect", "label": "single sign-on", "start": "auth/login",
                                  "problem": auth.MISCONFIGURED}]
    assert client.get("/auth/login?return=%23%2Fmfa").headers["Location"] == "../?signin=misconfigured#/mfa"
    for r in (client.get("/api/tokens"), client.post("/api/slots/1/reset-failures", json={})):
        assert r.status_code == 403 and r.get_json() == {"error": "Sign in to continue.", "token_required": True}


def test_only_admins_read_what_is_wrong_with_single_sign_on(db, monkeypatch, tmp_path):
    """The admin token and people who may see the settings (users.view) get the problem; it may name a path."""
    missing = str(tmp_path / "no-such-ca.pem")
    configure(monkeypatch, GRID_OIDC_CA_FILE=missing)
    problem = "GRID_OIDC_CA_FILE can't be read: there is no file {}.".format(missing)
    client = grid_app.app.test_client()

    def told(**kwargs):
        return [p.get("problem") for p in client.get("/api/session", **kwargs).get_json()["providers"]]

    assert told() == [None, auth.MISCONFIGURED]
    assert told(headers={"X-Grid-Token": ADMIN}) == [None, problem]
    sign_in_as(client, make_user(db, "Vic Viewer", [permissions.VIEWER]))
    assert told() == [None, auth.MISCONFIGURED]
    sign_in_as(client, make_user(db, "Rita Reader", [add_role(db, "Readers", "users.view")]))
    assert told() == [None, problem]
    assert client.get("/auth/login").headers["Location"] == "../?signin=misconfigured#/overview"


def test_a_database_without_the_users_table_is_a_problem_too(db, monkeypatch):
    configure(monkeypatch)
    run(db, "DROP TABLE users")
    client = grid_app.app.test_client()
    body = client.get("/api/session", headers={"X-Grid-Token": ADMIN}).get_json()
    assert [p.get("problem") for p in body["providers"]] == [None, accounts.NO_USER_TABLES]
    assert client.get("/auth/login").headers["Location"] == "../?signin=misconfigured#/overview"


def test_the_session_endpoint_during_an_outage(grid, db, monkeypatch, tmp_path):
    value = add_session(db, make_user(db, "Dana Lee", [permissions.ADMIN]))
    monkeypatch.setattr(store, "SQLITE_PATH", str(tmp_path))
    grid.set_cookie("grid-session", value)
    r = grid.get("/api/session")  # the session lookup meets the outage
    assert r.status_code == 200
    body = r.get_json()
    assert (body["signedIn"], body["unavailable"], body["permissions"]) == (False, True, [])
    assert cookie_set(r, "grid-session") is None  # the session may well be good: the cookie stays
    grid.delete_cookie("grid-session")
    body = grid.get("/api/session").get_json()  # nothing to look up: the check for the sign-in tables meets it
    assert (body["signedIn"], body["unavailable"]) == (False, True)


@pytest.mark.parametrize("admins, verified, admin", [
    ("dana@example.com", True, True),
    ("DANA@Example.com", True, True),
    ("dana@example.com", False, False),
    ("sub:" + SUBJECT, False, True),
    ("sub:" + SUBJECT.upper(), True, False),
])
def test_grid_oidc_admins_are_admins_from_their_first_sign_in(grid, idp, monkeypatch, admins, verified, admin):
    """An email counts only when the provider says it verified it; a subject matches exactly."""
    monkeypatch.setenv("GRID_OIDC_ADMINS", admins)
    idp.user["email_verified"] = verified
    through(grid, idp)
    body = grid.get("/api/session").get_json()
    assert body["admin"] is admin
    assert body["roles"] == ([{"id": permissions.ADMIN, "name": "Admin", "source": "env"}] if admin else [])


@pytest.mark.parametrize("change, kept, overage", [
    ({"groups": ["grid-testers", "grid-ops"]}, ["grid-testers", "grid-ops"], 0),
    ({"groups": "grid-ops"}, ["grid-ops"], 0),
    ({"groups": None}, [], 0),
    ({"overage": True}, [], 1),
])
def test_the_groups_a_sign_in_brings(grid, idp, db, change, kept, overage):
    for claim, value in change.items():
        if value is None:
            idp.user.pop(claim)
        else:
            idp.user[claim] = value
    through(grid, idp)
    groups, flag = run(db, "SELECT idpGroups, groupsOverage FROM users")[0]
    assert (json.loads(groups), flag) == (kept, overage)


def test_what_no_database_can_store_never_reaches_one(grid, idp, db):
    """JSON can carry a lone surrogate, and providers often let people edit their own name: the name loses it, an
    email or a group holding one is left out, and the sign-in works."""
    idp.user.update(name="Dana \ud800Lee", email="dana\ud800@example.com", groups=["grid-testers", "lone \ud800"])
    r = through(grid, idp)
    assert r.headers["Location"] == "../#/mfa" and cookie_set(r, "grid-session")
    assert run(db, "SELECT name, email, idpGroups FROM users") == [("Dana Lee", None, '["grid-testers"]')]
    assert run(db, "SELECT actorName FROM auditLog WHERE action = 'auth.signin'") == [("Dana Lee",)]


def test_a_group_mapping_gives_its_role_at_sign_in(grid, idp, db):
    run(db, "INSERT INTO groupRoles (groupName, roleId, createdAt) VALUES ('grid-testers', ?, ?)",
        (permissions.TESTER, ts(60)))
    through(grid, idp)
    body = grid.get("/api/session").get_json()
    assert body["roles"] == [{"id": permissions.TESTER, "name": "Tester", "source": "group", "group": "grid-testers"}]
    assert body["permissions"] == sorted(permissions.expand(permissions.BUILTIN_ROLES[permissions.TESTER][2]))


def test_signing_out(grid, idp, db):
    through(grid, idp)
    r = grid.post("/api/session/signout", json={})
    assert r.status_code == 200 and r.get_json() == {"signedOut": True} and r.headers["Cache-Control"] == "no-store"
    assert cookie_set(r, "grid-session")["max-age"] == "0"
    assert run(db, "SELECT revokedBy FROM userSessions WHERE revokedAt IS NOT NULL") == [("signed out",)]
    assert run(db, "SELECT actorName, action, details FROM auditLog WHERE action = 'auth.signout'") == [
        ("Dana Lee", "auth.signout", '{"everywhere":false}')]
    assert grid.get("/api/session").get_json()["signedIn"] is False
    assert grid.post("/api/session/signout", json={}).get_json() == {"signedOut": True}  # signed out already


def test_signing_out_everywhere_and_of_the_provider_too(grid, idp, db, monkeypatch):
    monkeypatch.setenv("GRID_OIDC_END_SESSION", "1")
    through(grid, idp)
    other = grid_app.app.test_client()
    through(other, idp)
    r = grid.post("/api/session/signout", json={"everywhere": True})
    url = urllib.parse.urlsplit(r.get_json()["endSessionUrl"])
    assert url._replace(query="").geturl() == idp.base_url + "/logout"
    assert urllib.parse.parse_qs(url.query) == {"client_id": ["grid-ui"],
                                                "post_logout_redirect_uri": ["http://localhost/"]}
    assert count(db, "userSessions") == 2
    assert run(db, "SELECT COUNT(*) FROM userSessions WHERE revokedAt IS NULL") == [(0,)]
    assert other.get("/api/tokens").get_json()["reason"] == "revoked"


def test_signing_out_takes_the_page_s_own_requests_only(grid, idp, db):
    through(grid, idp)
    r = grid.post("/api/session/signout", data="everywhere=1", content_type="application/x-www-form-urlencoded")
    assert r.status_code == 403
    r = grid.post("/api/session/signout", json={}, headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403
    assert run(db, "SELECT COUNT(*) FROM userSessions WHERE revokedAt IS NULL") == [(1,)]
    assert grid.get("/api/session").get_json()["signedIn"] is True


def test_the_public_address_may_manage_the_grid(grid, monkeypatch):
    """GRID_UI_PUBLIC_URL's host counts as one in GRID_UI_ALLOWED_HOSTS; another site still doesn't."""
    monkeypatch.setattr(grid_app, "ALLOWED_HOSTS", set())
    public = {"base_url": "https://grid.example.com"}
    r = grid.post("/api/session/signout", json={}, **public)
    assert r.status_code == 403 and r.get_json() == {
        "error": "Actions aren't accepted through the host name 'grid.example.com'. Add it to GRID_UI_ALLOWED_HOSTS to "
                 "manage the grid through it."}
    monkeypatch.setenv("GRID_UI_PUBLIC_URL", "https://grid.example.com/")
    assert grid.post("/api/session/signout", json={}, **public).status_code == 200
    # Behind a proxy that rewrites Host, the page's Origin is the public address.
    assert grid.post("/api/session/signout", json={}, base_url="http://10.0.0.5:4444",
                     headers={"Origin": "https://grid.example.com"}).status_code == 200
    r = grid.post("/api/session/signout", json={}, headers={"Origin": "https://evil.example.com"}, **public)
    assert r.status_code == 403 and r.get_json() == {"error": "Cross-origin request refused."}


@pytest.mark.parametrize("hops, address", [(1, "203.0.113.9"), (0, "127.0.0.1")])
def test_proxy_hops_decide_whose_address_a_session_keeps(grid, idp, db, monkeypatch, hops, address):
    monkeypatch.setattr(grid_app, "PROXY_HOPS", hops)
    through(grid, idp, headers={"X-Forwarded-For": "203.0.113.9"})
    assert run(db, "SELECT address FROM userSessions") == [(address,)]
    assert run(db, "SELECT address FROM auditLog WHERE action = 'auth.signin'") == [(address,)]


def test_nothing_secret_reaches_an_answer_or_the_log(grid, idp, db, monkeypatch, caplog):
    """The code, state, nonce, PKCE verifier, the provider's tokens, the client secret and the session cookie's value
    are in no answer (bar the cookie itself, and the authorization request, which carries the state and nonce to
    the provider) and in no log line, whether the sign-in works or not."""
    caplog.set_level(logging.DEBUG)
    answers, fetch = [], oidc._fetch

    def recorded(url, **kwargs):
        document = fetch(url, **kwargs)
        answers.append(document)
        return document

    monkeypatch.setattr(oidc, "_fetch", recorded)
    hidden, sent_to_provider, responses, authorize = {fake_idp.CLIENT_SECRET}, set(), [], []

    def sign_in(fault=None, deny=False):
        r = start(grid)
        authorize.append(r)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["Location"]).query)
        sent_to_provider.update([query["state"][0], query["nonce"][0]])
        hidden.add(run(db, "SELECT codeVerifier FROM oidcLogins ORDER BY id DESC LIMIT 1")[0][0])
        idp.fault = fault
        back = (idp.deny if deny else idp.approve)(r.headers["Location"])
        hidden.update(urllib.parse.parse_qs(urllib.parse.urlsplit(back).query).get("code", []))
        r = grid.get(local(back))
        responses.append(r)
        session = cookie_set(r, "grid-session")
        if session:
            hidden.add(session["value"])

    # The provider's user signs in as an admin, so the Admin page's and the sharing routes answer in full.
    monkeypatch.setenv("GRID_OIDC_ADMINS", "sub:" + SUBJECT)
    secret, signin = add_item(db, "mfa", "intranet"), add_item(db, "signin", "prod-admin")
    sign_in()
    ((dana,),) = run(db, "SELECT id FROM users")
    other = make_user(db, "Bob Stone", [permissions.TESTER])
    hidden.add(add_session(db, other))
    responses.extend([grid.get("/api/session"), grid.get("/api/tokens"), grid.get("/api/directory?q=da"),
                      grid.post("/api/mfa/{}/shares/add".format(secret), json={"role": permissions.TESTER}),
                      grid.post("/api/signins/{}/shares/add".format(signin), json={"user": other}),
                      grid.post("/api/signins/{}/owner".format(signin), json={"userId": other}),
                      grid.post("/api/admin/roles", json={"name": "QA", "permissions": ["mfa.add"]}),
                      grid.post("/api/admin/groups", json={"group": "grid-testers", "roleId": permissions.TESTER}),
                      grid.post("/api/admin/users/{}/roles/add".format(other), json={"role": permissions.VIEWER}),
                      grid.post("/api/admin/users/{}/sessions/revoke".format(other), json={}),
                      grid.post("/api/admin/users/{}/disable".format(other), json={"revokeClientTokens": True}),
                      grid.get("/api/admin/users?status=all"), grid.get("/api/admin/users/{}".format(dana)),
                      grid.get("/api/admin/users/{}".format(other)), grid.get("/api/admin/roles"),
                      grid.get("/api/admin/groups"), grid.get("/api/admin/audit"), grid.get("/api/admin/sign-in")])
    assert all(r.status_code in (200, 201) for r in responses[1:]), [r.get_data(as_text=True) for r in responses]
    responses.append(grid.post("/api/session/signout", json={}))
    for fault in ("wrong_nonce", "expired", "token_400_invalid_grant"):
        sign_in(fault)
    sign_in(deny=True)
    tokens = [document[k] for document in answers for k in ("id_token", "access_token", "refresh_token")
              if k in document]
    assert len(tokens) == 3 * 3  # from the sign-in that worked, wrong_nonce's and expired's
    hidden.update(tokens)

    def shown(r):
        headers = "".join("{}: {}\n".format(k, v) for k, v in r.headers.items() if k.lower() != "set-cookie")
        return headers + r.get_data(as_text=True)

    everything = "".join(shown(r) for r in responses) + caplog.text
    for value in hidden | sent_to_provider:
        assert value not in everything
    for r in authorize:
        assert not any(value in shown(r) for value in hidden)
