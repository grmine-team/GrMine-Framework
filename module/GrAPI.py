import os
import json
import sys
import importlib
from typing import Dict, List, Optional, Any, Union

import yaml

from module import zipimport
from module import tools
from module.plugin_importer import PluginImporter


class GrAPI:
    def __init__(self, name):
        if name is None or not hasattr(name, '__class__') or name.__class__.__name__ != 'PluginAPI':
            tools.Console("Server Thread/Plugin loader").error("请不要直接执行插件主文件")
            sys.exit(-1)
        self.plugin_api = name
        self.plugins: Dict = {}
        self.importer: zipimport.zipimporter = self.plugin_api.__importer__
        self.info: Dict = self.plugin_api.info
        self.console: tools.Console = self.plugin_api.console
        self.app = self.plugin_api.app
        self._libs_prefix: Optional[str] = self.plugin_api._libs_prefix

    def get_plugin_data(self, file_path: str) -> bytes:
        return self.importer.get_data(file_path)

    def read_config(self, config_file: str, config_type: str = "json") -> Union[Dict, str]:
        package_name = self.info.get("package_name", self.info.get("plugin_name", "default"))
        config_path = os.path.join("./plugins", package_name, config_file)
        
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")
        
        with open(config_path, "r", encoding="utf-8") as f:
            if config_type == "json":
                return json.load(f)
            elif config_type == "yaml" or config_type == "yml":
                return yaml.load(f, Loader=yaml.FullLoader)
            else:
                return f.read()

    def write_config(self, config_file: str, config: Union[Dict, str], config_type: str = "json") -> None:
        package_name = self.info.get("package_name", self.info.get("plugin_name", "default"))
        config_dir = os.path.join("./plugins", package_name)
        
        if not os.path.exists(config_dir):
            os.makedirs(config_dir)
        
        config_path = os.path.join(config_dir, config_file)
        
        with open(config_path, "w", encoding="utf-8") as f:
            if isinstance(config, str):
                f.write(config)
            elif config_type == "json":
                json.dump(config, f, indent=2, ensure_ascii=False)
            elif config_type == "yaml" or config_type == "yml":
                yaml.dump(config, f, allow_unicode=True)
            else:
                f.write(str(config))

    def exist_config(self, config_file: str) -> bool:
        package_name = self.info.get("package_name", self.info.get("plugin_name", "default"))
        config_path = os.path.join("./plugins", package_name, config_file)
        return os.path.exists(config_path)

    def load_plugin_module(self, module_name: str):
        return self.importer.load_module(module_name)

    def get_plugin(self, package_name: str) -> Any:
        return self.plugin_api.get_plugin(package_name)

    def get_bundled_module(self, module_name: str) -> Any:
        if not self._libs_prefix:
            raise ImportError("Plugin does not have bundled libraries support")
        
        full_module_name = f"{self._libs_prefix}.{module_name}"
        
        if full_module_name in sys.modules:
            return sys.modules[full_module_name]
        
        return importlib.import_module(full_module_name)

    def list_bundled_modules(self) -> List[str]:
        package_name = self.info.get("package_name")
        if not package_name:
            return []
        return PluginImporter.list_bundled_modules(package_name)

    def get_plugin_path(self) -> str:
        return getattr(self.importer, 'archive', '')

    def get_data_file(self, file_path: str) -> bytes:
        try:
            return self.importer.get_data(file_path)
        except OSError:
            raise FileNotFoundError(f"File not found in plugin: {file_path}")

    def get_data_text(self, file_path: str, encoding: str = "utf-8") -> str:
        return self.get_data_file(file_path).decode(encoding)

    def has_data_file(self, file_path: str) -> bool:
        try:
            self.importer.get_data(file_path)
            return True
        except OSError:
            return False

    def get_plugin_info(self) -> Dict:
        return self.info.copy()

    def get_plugin_name(self) -> str:
        return self.info.get("plugin_name", "Unknown")

    def get_plugin_version(self) -> str:
        return self.info.get("version", "?.?.?")

    def get_plugin_author(self) -> str:
        return self.info.get("author", "Unknown")

    def __str__(self):
        return f"<GrAPI: {self.get_plugin_name()}>"

    def __repr__(self):
        return self.__str__()
