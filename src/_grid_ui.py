"""What src/mfa.py and src/signin.py share: where the Grid UI is, the client token to ask it with, and one
JSON request to it. The helpers turn Refused and Unreachable into their own errors."""
import json
import os
import urllib.error
import urllib.request

DEFAULT_URL = "http://grid_ui:4444"


class Refused(Exception):
    """The Grid UI answered with an HTTP error: its status, its JSON answer ({} when it sent none) and the
    HTTP reason. The answer says why: a 403 has not_shared, forbidden, owner_disabled, or out_of_scope (with the
    scope id as "scope") for a token whose scope leaves the request out."""

    def __init__(self, status, answer, reason):
        super().__init__(status, reason)
        self.status = status
        self.answer = answer
        self.reason = reason


class Unreachable(Exception):
    """The Grid UI could not be reached. Holds the reason only, never the request."""


def settings(url, token, error):
    """The Grid UI's URL and the client token: the arguments, else the environment. Raises error(message)
    when there is no token."""
    url = (url or os.environ.get("GRID_UI_URL") or DEFAULT_URL).rstrip("/")
    token = token or os.environ.get("GRID_MFA_TOKEN") or os.environ.get("GATEWAY_CLIENT_TOKEN") or ""
    if not token:
        raise error("No client token to ask the Grid UI with: set GRID_MFA_TOKEN or GATEWAY_CLIENT_TOKEN.")
    return url, token


def post(url, token, path, body, timeout_s):
    """POSTs body as JSON to the Grid UI's path with the client token and returns its JSON answer."""
    request = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json", "Authorization": "Bearer " + token})
    # Raised from None: urllib's errors hold the request, and with it the token.
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        try:
            answer = json.load(e)
        except ValueError:
            answer = {}
        raise Refused(e.code, answer if isinstance(answer, dict) else {}, e.reason) from None
    except (urllib.error.URLError, OSError) as e:
        raise Unreachable(getattr(e, "reason", e)) from None
