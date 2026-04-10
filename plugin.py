import importlib
import os
import json
import queue
import hashlib
import subprocess
import threading
import traceback
from typing import Dict, List, Optional, Any

from module.tools import Console
from module import zipimport
from module.plugin_importer import PluginImporter


console = Console("Server Thread/Plugin loader")


class PluginAPI:
    def __init__(self, info: Dict, importer: zipimport.zipimporter, app: Any, plugins: Dict):
        self.info = info
        self.console = Console(info.get("plugin_name", "Unknown"))
        self.__importer__ = importer
        self.app = app
        self.__plugins = plugins
        self._libs_prefix: Optional[str] = None

    def get_plugin(self, package_name: str) -> Any:
        if package_name not in self.info.get("dependent_plugin", []):
            raise KeyError(f"You are not allowed to get plugin \"{package_name}\"")
        if package_name not in self.__plugins:
            raise KeyError(f"Plugin \"{package_name}\" is not loaded")
        return self.__plugins[package_name]

    def get_libs_prefix(self) -> Optional[str]:
        return self._libs_prefix

    def __str__(self):
        return f"<PluginAPI: {self.info.get('plugin_name', 'Unknown')}>"

    def __repr__(self):
        return self.__str__()


class PluginLoadError(Exception):
    pass


class PluginDependencyError(Exception):
    pass


class Plugin:
    def __init__(self, app: Any, pip_path: str):
        self.plugin_dict: Dict[str, Dict[str, Any]] = {}
        self.loaded: List[str] = []
        self.app = app
        self.pip_path = pip_path
        self._load_errors: List[str] = []
        
        PluginImporter.get_instance()

    def get_plugins(self) -> List[str]:
        dependent_plugin: Dict[str, List[str]] = {}
        in_degree: Dict[str, int] = {}
        
        for parent, dir_names, file_names in os.walk('./plugins'):
            file_names[:] = [f for f in file_names if f.endswith(".grpl")]
            for filename in file_names:
                filepath = os.path.join(parent, filename)
                try:
                    plugin_zip = zipimport.zipimporter(filepath)
                    info_data = plugin_zip.get_data("info.json").decode("utf-8")
                    info = json.loads(info_data)
                except Exception as e:
                    console.error(f"Failed to read plugin \"{filename}\": {e}")
                    continue
                
                package_name = info.get("package_name")
                if not package_name:
                    console.error(f"Plugin \"{filename}\" has no package_name")
                    continue
                
                if package_name in self.plugin_dict:
                    console.warning(f"Duplicate plugin \"{package_name}\", skipping")
                    continue
                
                in_degree[package_name] = 0
                self.plugin_dict[package_name] = {
                    "zip": plugin_zip, 
                    "plugin": None, 
                    "info": info,
                    "filepath": filepath
                }
                
                deps = info.get("dependent_plugin", [])
                for dep in deps:
                    if dep not in dependent_plugin:
                        dependent_plugin[dep] = []
                    dependent_plugin[dep].append(package_name)
                    in_degree[package_name] = in_degree.get(package_name, 0) + 1
                
                del info
                del plugin_zip
        
        q = queue.Queue()
        for plugin_name in in_degree:
            if in_degree[plugin_name] == 0:
                q.put(plugin_name)
        
        load_order = []
        while not q.empty():
            u = q.get()
            load_order.append(u)
            
            for v in dependent_plugin.get(u, []):
                in_degree[v] -= 1
                if in_degree[v] == 0:
                    q.put(v)
        
        for plugin_name in load_order:
            if plugin_name in self.plugin_dict and plugin_name not in self.loaded:
                self._load_plugin(plugin_name)
        
        for plugin_name in in_degree:
            if in_degree[plugin_name] > 0 and plugin_name in self.plugin_dict:
                console.error(f"Plugin \"{plugin_name}\" has unresolved dependencies")
        
        threading.Thread(target=self._call_loaded_hooks, daemon=True).start()
        
        return self.loaded

    def _load_plugin(self, package_name: str) -> bool:
        if package_name in self.loaded:
            return True
        
        plugin_data = self.plugin_dict.get(package_name)
        if not plugin_data:
            console.error(f"Plugin \"{package_name}\" not found")
            return False
        
        try:
            plugin_zip = plugin_data["zip"]
            info: Dict = plugin_data["info"]
            
            deps = info.get("dependent_plugin", [])
            for dep in deps:
                if dep not in self.loaded:
                    if dep not in self.plugin_dict:
                        raise PluginDependencyError(
                            f"Missing dependency: \"{dep}\""
                        )
                    if not self._load_plugin(dep):
                        raise PluginDependencyError(
                            f"Failed to load dependency: \"{dep}\""
                        )
            
            libs_prefix = PluginImporter.register_plugin(package_name, plugin_zip)
            
            modules = info.get("modules", [])
            for module_info in modules:
                if not isinstance(module_info, dict):
                    continue
                
                bundled = module_info.get("bundled", False)
                import_name = module_info.get("import_name")
                
                if bundled and import_name:
                    plugin_importer_info = PluginImporter.get_instance()._plugin_importers.get(package_name)
                    bundled_modules = plugin_importer_info.bundled_modules if plugin_importer_info else set()
                    if import_name in bundled_modules:
                        continue
                    console.warning(f"Bundled module '{import_name}' not found in libs, falling back to pip install")
                
                if import_name:
                    try:
                        importlib.import_module(import_name)
                    except ImportError:
                        if self._install_module(module_info):
                            importlib.invalidate_caches()
                            try:
                                importlib.import_module(import_name)
                            except ImportError as e:
                                console.error(f"Failed to import {import_name} after installation: {e}")
                        else:
                            console.error(f"Failed to install {import_name}")
                            raise PluginDependencyError(f"Failed to install dependency: {import_name}")
            
            plugin_api = PluginAPI(info, plugin_zip, self.app, self.plugin_dict)
            plugin_api._libs_prefix = libs_prefix
            
            entrance = info.get("entrance", "main")
            module_name = f"grapi.plugins.{hashlib.sha256(package_name.encode()).hexdigest()}"
            
            PluginImporter.set_current_plugin(package_name)
            try:
                load_plugin = plugin_zip.load_module(
                    entrance, 
                    {
                        "__doc__": plugin_api, 
                        "print": plugin_api.console.info,
                        "__plugin_api__": plugin_api
                    },
                    module_name
                )
            finally:
                PluginImporter.set_current_plugin(None)
            
            plugin_data["plugin"] = load_plugin
            plugin_data["plugin_api"] = plugin_api
            self.loaded.append(package_name)
            
            console.info(f"Loaded plugin: {info.get('plugin_name', package_name)} v{info.get('version', '?.?.?')}")
            return True
            
        except PluginDependencyError as e:
            console.error(f"Dependency error in \"{package_name}\": {e}")
            self._load_errors.append(f"{package_name}: {e}")
            return False
        except Exception as e:
            console.error(f"Failed to load plugin \"{package_name}\"")
            console.error(traceback.format_exc().strip())
            self._load_errors.append(f"{package_name}: {e}")
            return False

    def _install_module(self, module_info: Dict) -> bool:
        module_name = module_info.get("module_name")
        if not module_name:
            return False
        
        try:
            console.info(f"Installing module: {module_name}")
            result = subprocess.run(
                [self.pip_path, "install", module_name],
                capture_output=True,
                text=True,
                timeout=300
            )
            if result.returncode != 0:
                console.error(f"Failed to install {module_name}: {result.stderr}")
                return False
            return True
        except subprocess.TimeoutExpired:
            console.error(f"Timeout installing {module_name}")
            return False
        except Exception as e:
            console.error(f"Error installing {module_name}: {e}")
            return False

    def _call_loaded_hooks(self):
        for package_name in self.loaded:
            plugin_data = self.plugin_dict.get(package_name)
            if not plugin_data:
                continue
            
            plugin = plugin_data.get("plugin")
            if plugin and hasattr(plugin, 'loaded'):
                try:
                    plugin.loaded()
                except Exception as e:
                    console.warning(f"Error in loaded hook for \"{package_name}\": {e}")

    def unload_plugin(self, package_name: str) -> bool:
        if package_name not in self.loaded:
            return False
        
        plugin_data = self.plugin_dict.get(package_name)
        if not plugin_data:
            return False
        
        plugin = plugin_data.get("plugin")
        if plugin and hasattr(plugin, 'unload'):
            try:
                plugin.unload()
            except Exception as e:
                console.warning(f"Error in unload hook for \"{package_name}\": {e}")
        
        PluginImporter.unregister_plugin(package_name)
        
        self.loaded.remove(package_name)
        plugin_data["plugin"] = None
        
        console.info(f"Unloaded plugin: {package_name}")
        return True

    def get_plugin_info(self, package_name: str) -> Optional[Dict]:
        plugin_data = self.plugin_dict.get(package_name)
        if plugin_data:
            return plugin_data.get("info")
        return None

    def list_plugins(self) -> List[Dict]:
        result = []
        for package_name, data in self.plugin_dict.items():
            info = data.get("info", {})
            result.append({
                "package_name": package_name,
                "plugin_name": info.get("plugin_name", "Unknown"),
                "version": info.get("version", "?.?.?"),
                "loaded": package_name in self.loaded,
                "author": info.get("author", "Unknown")
            })
        return result

    def get_load_errors(self) -> List[str]:
        return self._load_errors.copy()
