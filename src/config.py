import os
import yaml

class config:

    __file = "/app/src/database.yaml"
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
            return yaml.safe_load(stream)

    def get_mysql_config(self):
        settings = dict(self.__config.get("mysql") or {})
        for key, env in self.__env_overrides.items():
            if os.environ.get(env):
                settings[key] = os.environ[env]
        settings["port"] = int(settings.get("port", 3306))
        return settings

    def get_hosts_config(self):
        return self.__config["hosts"]
