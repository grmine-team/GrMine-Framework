import sys
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import importlib
import threading
import types
import platform
import tempfile
import os
from dataclasses import dataclass, field
from typing import Dict, Optional, Any, Set, Tuple, List

from module import zipimport


def _get_current_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == 'windows':
        return 'win-amd64'
    if system == 'darwin':
        return 'macos-arm64' if machine in ('arm64', 'aarch64') else 'macos-x86_64'
    return 'linux-aarch64' if machine in ('aarch64', 'arm64') else 'linux-x86_64'


@dataclass
class PluginInfo:
    zip_importer: zipimport.zipimporter
    libs_prefix: str
    plugin_hash: str
    loaded_modules: Dict[str, types.ModuleType] = field(default_factory=dict)
    bundled_modules: Set[str] = field(default_factory=set)
    platform_modules: Dict[str, str] = field(default_factory=dict)


class PluginImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    _instance: Optional['PluginImporter'] = None
    _current_platform: str = _get_current_platform()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._plugin_importers: Dict[str, PluginInfo] = {}
            cls._instance._loaded_bundled: Dict[str, str] = {}
            cls._instance._current_plugin = threading.local()
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

    @classmethod
    def get_instance(cls) -> 'PluginImporter':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def register_plugin(cls, package_name: str, zip_importer: zipimport.zipimporter) -> str:
        instance = cls.get_instance()
        plugin_hash = hashlib.sha256(package_name.encode()).hexdigest()[:16]
        libs_prefix = f"grapi.plugin_libs.{plugin_hash}"
        bundled_modules, platform_modules = cls._scan_bundled_modules(zip_importer)
        instance._plugin_importers[package_name] = PluginInfo(
            zip_importer=zip_importer,
            libs_prefix=libs_prefix,
            plugin_hash=plugin_hash,
            bundled_modules=bundled_modules,
            platform_modules=platform_modules,
        )
        if instance not in sys.meta_path:
            sys.meta_path.insert(0, instance)
        return libs_prefix

    @classmethod
    def _scan_bundled_modules(cls, zip_imp: zipimport.zipimporter) -> Tuple[Set[str], Dict[str, str]]:
        bundled: Set[str] = set()
        platform_modules: Dict[str, str] = {}
        current_platform = cls._current_platform
        platform_prefix = f"{current_platform}/"
        try:
            files = zip_imp._files
            for file_path in files:
                normalized = file_path.replace('\\', '/')
                if not normalized.startswith('libs/'):
                    continue
                remaining = normalized[5:]
                is_platform_specific = remaining.startswith(platform_prefix)
                if is_platform_specific:
                    remaining = remaining[len(platform_prefix):]
                if not (remaining.endswith('.py') or remaining.endswith('.pyd') or remaining.endswith('.so')):
                    continue
                if not remaining:
                    continue
                module_name = remaining.rsplit('.', 1)[0].replace('/', '.')
                if module_name.endswith('.__init__'):
                    module_name = module_name[:-9]
                parts = module_name.split('.')
                if len(parts) > 1 and parts[-1].startswith('cp'):
                    module_name = '.'.join(parts[:-1])
                if not module_name:
                    continue
                is_binary = remaining.endswith(('.pyd', '.so'))
                for i in range(len(parts)):
                    bundled.add('.'.join(parts[:i + 1]))
                if is_platform_specific and is_binary:
                    resolved = '.'.join(parts[:-1]) if len(parts) > 1 and parts[-1].startswith('cp') else module_name
                    if resolved not in platform_modules:
                        platform_modules[resolved] = file_path
        except Exception:
            pass
        return bundled, platform_modules

    @classmethod
    def unregister_plugin(cls, package_name: str) -> bool:
        instance = cls.get_instance()
        if package_name not in instance._plugin_importers:
            return False
        plugin_info = instance._plugin_importers[package_name]
        for mod_name in list(plugin_info.loaded_modules.keys()):
            sys.modules.pop(mod_name, None)
            instance._loaded_bundled.pop(mod_name, None)
        del instance._plugin_importers[package_name]
        return True

    @classmethod
    def get_libs_prefix(cls, package_name: str) -> Optional[str]:
        instance = cls.get_instance()
        if package_name in instance._plugin_importers:
            return instance._plugin_importers[package_name].libs_prefix
        return None

    @classmethod
    def set_current_plugin(cls, package_name: Optional[str]) -> None:
        instance = cls.get_instance()
        instance._current_plugin.name = package_name

    @classmethod
    def get_current_plugin(cls) -> Optional[str]:
        instance = cls.get_instance()
        return getattr(instance._current_plugin, 'name', None)

    def find_spec(self, fullname: str, path, target=None):
        current_plugin = self.get_current_plugin()
        if current_plugin and current_plugin in self._plugin_importers:
            plugin_info = self._plugin_importers[current_plugin]
            if fullname in plugin_info.bundled_modules:
                return importlib.machinery.ModuleSpec(
                    fullname, self,
                    origin=f"<plugin:{current_plugin}:libs/{fullname.replace('.', '/')}>"
                )
        return None

    def load_module(self, fullname: str):
        if fullname in sys.modules:
            return sys.modules[fullname]
        current_plugin = self.get_current_plugin()
        if not current_plugin or current_plugin not in self._plugin_importers:
            raise ImportError(f"Cannot load module {fullname}: no current plugin context")
        plugin_info = self._plugin_importers[current_plugin]
        module = self._load_module_from_zip(plugin_info.zip_importer, fullname, fullname, current_plugin, plugin_info.platform_modules)
        if module is not None:
            plugin_info.loaded_modules[fullname] = module
            self._loaded_bundled[fullname] = current_plugin
        return module

    def _build_libs_paths(self, module_path_parts: List[str]) -> List[str]:
        parts_joined = "/".join(module_path_parts)
        return [
            f"libs/{self._current_platform}/{parts_joined}",
            f"libs/{parts_joined}",
        ]

    def _try_get_data(self, zip_imp: zipimport.zipimporter, path: str) -> Optional[bytes]:
        try:
            return zip_imp.get_data(path)
        except OSError:
            return None

    def _find_package_init(self, zip_imp: zipimport.zipimporter, libs_paths: List[str]) -> Tuple[Optional[bytes], Optional[str]]:
        for libs_path in libs_paths:
            init_py = f"{libs_path}/__init__.py"
            code_data = self._try_get_data(zip_imp, init_py)
            if code_data is not None:
                return code_data, init_py
        return None, None

    def _find_module_py(self, zip_imp: zipimport.zipimporter, libs_paths: List[str]) -> Tuple[Optional[bytes], Optional[str]]:
        for libs_path in libs_paths:
            module_py = f"{libs_path}.py"
            code_data = self._try_get_data(zip_imp, module_py)
            if code_data is not None:
                return code_data, module_py
        return None, None

    def _find_extension_module(self, zip_imp: zipimport.zipimporter, libs_paths: List[str], fullname: str, platform_modules: Dict[str, str]) -> Tuple[Optional[bytes], Optional[str]]:
        ext_suffix = '.pyd' if platform.system() == 'Windows' else '.so'
        if fullname in platform_modules:
            ext_binary = self._try_get_data(zip_imp, platform_modules[fullname])
            if ext_binary is not None:
                return ext_binary, platform_modules[fullname]
        for libs_path in libs_paths:
            for f in zip_imp._files:
                f_norm = f.replace('\\', '/')
                if f_norm.startswith(libs_path + '.') and f_norm.endswith(ext_suffix):
                    ext_binary = self._try_get_data(zip_imp, f)
                    if ext_binary is not None:
                        return ext_binary, f
        return None, None

    def _find_implicit_package(self, zip_imp: zipimport.zipimporter, libs_paths: List[str]) -> Tuple[Optional[bytes], Optional[str]]:
        for libs_path in libs_paths:
            prefix = f"{libs_path}/"
            for f in zip_imp._files:
                if f.replace('\\', '/').startswith(prefix) and f.endswith('.py'):
                    return b"", f"{libs_path}/__init__.py"
        return None, None

    def _load_module_from_zip(self, zip_imp, module_path: str, fullname: str, package_name: str, platform_modules: Dict[str, str]):
        module_path_parts = module_path.split('.')
        libs_paths = self._build_libs_paths(module_path_parts)

        code_data, file_path = self._find_package_init(zip_imp, libs_paths)
        is_package = code_data is not None

        if code_data is None:
            code_data, file_path = self._find_module_py(zip_imp, libs_paths)
            is_package = False

        is_extension = False
        ext_binary = None
        if code_data is None:
            ext_binary, file_path = self._find_extension_module(zip_imp, libs_paths, fullname, platform_modules)
            is_extension = ext_binary is not None

        if code_data is None and ext_binary is None:
            code_data, file_path = self._find_implicit_package(zip_imp, libs_paths)
            is_package = code_data is not None

        if code_data is None and ext_binary is None:
            raise ImportError(f"Module {module_path} not found in plugin {package_name}")

        if is_extension and ext_binary is not None:
            return self._load_extension_module(ext_binary, file_path, fullname)

        return self._load_python_module(code_data, file_path, fullname, is_package, package_name)

    def _load_extension_module(self, ext_binary: bytes, file_path: str, fullname: str):
        temp_dir = tempfile.mkdtemp(prefix='grmine_ext_')
        ext_filename = os.path.basename(file_path.replace('\\', '/'))
        temp_ext_path = os.path.join(temp_dir, ext_filename)
        with open(temp_ext_path, 'wb') as f:
            f.write(ext_binary)
        spec = importlib.util.spec_from_file_location(fullname, temp_ext_path)
        if spec and spec.loader:
            ext_module = importlib.util.module_from_spec(spec)
            sys.modules[fullname] = ext_module
            spec.loader.exec_module(ext_module)
            ext_module.__file__ = "<zip>" + file_path
            ext_module.__loader__ = self
            return ext_module
        raise ImportError(f"Cannot create spec for extension module {fullname}")

    def _load_python_module(self, code_data: bytes, file_path: str, fullname: str, is_package: bool, package_name: str):
        module = types.ModuleType(fullname)
        module.__file__ = "<zip>" + file_path
        module.__loader__ = self
        module.__package__ = fullname if is_package else fullname.rpartition('.')[0]
        if is_package:
            module.__path__ = []
        spec = importlib.machinery.ModuleSpec(
            fullname, self,
            origin="<zip>" + file_path,
            is_package=is_package,
        )
        if is_package:
            spec.submodule_search_locations = []
        module.__spec__ = spec
        sys.modules[fullname] = module
        try:
            if code_data:
                code = compile(code_data.decode('utf-8'), file_path, 'exec')
                old_plugin = self.get_current_plugin()
                if old_plugin != package_name:
                    self.set_current_plugin(package_name)
                try:
                    exec(code, module.__dict__)
                finally:
                    if old_plugin != package_name:
                        self.set_current_plugin(old_plugin)
        except Exception as e:
            sys.modules.pop(fullname, None)
            raise ImportError(f"Error loading module {fullname}: {e}")
        return module

    @classmethod
    def list_bundled_modules(cls, package_name: str) -> List[str]:
        instance = cls.get_instance()
        if package_name not in instance._plugin_importers:
            return []
        return list(instance._plugin_importers[package_name].bundled_modules)

    @classmethod
    def is_registered(cls, package_name: str) -> bool:
        return package_name in cls.get_instance()._plugin_importers

    @classmethod
    def get_bundled_module(cls, package_name: str, module_name: str) -> Any:
        if module_name in sys.modules:
            return sys.modules[module_name]
        instance = cls.get_instance()
        if package_name not in instance._plugin_importers:
            raise ImportError(f"Plugin {package_name} not registered")
        return importlib.import_module(module_name)

    @classmethod
    def get_current_platform(cls) -> str:
        return cls._current_platform


def init_plugin_importer():
    return PluginImporter.get_instance()
