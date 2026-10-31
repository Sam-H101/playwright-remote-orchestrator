import os
import yaml

DEFAULT_GATEWAY_URL = "ws://hub:4500"


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
        return {"url": url.rstrip("/"), "client_token": token}
