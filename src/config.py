import yaml
import pkgutil

class config:

    __file = "/app/src/database.yaml"
    __config = {}
    def __init__(self):
        self.__config = self.get_config()
    def get_config(self):
        with open(self.__file, "r") as stream:
            return yaml.safe_load(stream)

    def get_db_file_file(self):
        return self.__config["dbfile"]

    def get_hosts_config(self):
        return self.__config["hosts"]
