import os
import re
from urllib.parse import quote, urlencode, urlsplit

import yaml

DEFAULT_GATEWAY_URL = "ws://hub:4500"
# The hub's node-name rule (case-insensitive).
NODE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def client_endpoint(url, client_token=None, node=None):
    """ the hub's /connect URL. The hub picks a free slot for the browser the check launches, on
    `node` if one is given; there is no way to name a slot. """
    if node and not NODE_NAME.match(node):
        raise ValueError("GATEWAY_NODE must be a node name ([A-Za-z0-9._-], up to 128 characters), got {!r}"
                         .format(node))
    base = url.rstrip("/")
    path = urlsplit(base).path
    if "/connect/" in path:
        # The hub answers the removed /connect/<node>/<slot> endpoints with 410.
        raise ValueError("GATEWAY_URL must be the hub's address (e.g. ws://hub:4500), not a /connect/<node>/<slot> "
                         "endpoint like {!r}; set GATEWAY_NODE to stay on one node".format(path))
    # A GATEWAY_URL copied from a client snippet may already end in /connect.
    if base.endswith("/connect"):
        base = base[:-len("/connect")]
    query = [(key, value) for key, value in (("gateway_token", client_token), ("node", node)) if value]
    return base + "/connect" + ("?" + urlencode(query, quote_via=quote, safe="") if query else "")


class config:

    # Next to this module, so it works both in /app/src and in a checkout.
    __file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.yaml")
    __config = {}
    __env_overrides = {
        "host": "DB_HOST",
        "port": "DB_PORT",
        "user": "DB_USER",
        "password": "DB_PASSWORD",
        "database": "DB_NAME",
    }

    def __init__(self):
        self.__config = self.get_config()

    def get_config(self):
        path = os.environ.get("ORCHESTRATOR_CONFIG", self.__file)
        with open(path, "r") as stream:
            return yaml.safe_load(stream) or {}

    def get_mysql_config(self):
        settings = dict(self.__config.get("mysql") or {})
        for key, env in self.__env_overrides.items():
            if os.environ.get(env):
                settings[key] = os.environ[env]
        settings["port"] = int(settings.get("port", 3306))
        return settings

    def get_hosts_config(self):
        """ legacy static hosts as (server, port_start, port_end); blank server names are skipped """
        hosts = []
        for entry in self.__config.get("hosts") or []:
            server = str((entry or {}).get("server") or "").strip()
            if server:
                hosts.append((server, int(entry["port_start"]), int(entry["port_end"])))
        return hosts

    def get_gateway_config(self):
        settings = dict(self.__config.get("gateway") or {})
        url = os.environ.get("GATEWAY_URL") or settings.get("url") or DEFAULT_GATEWAY_URL
        token = os.environ.get("GATEWAY_CLIENT_TOKEN") or settings.get("client_token") or None
        node = os.environ.get("GATEWAY_NODE") or settings.get("node") or None
        return {"url": url.rstrip("/"), "client_token": token, "node": str(node).strip() if node else None}
