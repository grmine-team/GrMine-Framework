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
import struct
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
    # 预解压的平台二进制文件临时目录
    ext_temp_dir: Optional[str] = None
    # DLL 搜索目录令牌 (Windows add_dll_directory)
    dll_directory_tokens: list = field(default_factory=list)


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

        # 只要有 bundled 模块就预解压到临时目录
        # 原因：即使没有 C++ 扩展，包内也可能有非 Python 资源文件（如 .lua, .json 等），
        # importlib.resources 需要通过文件系统路径来访问这些资源
        ext_temp_dir = None
        dll_directory_tokens = []
        if bundled_modules:
            ext_temp_dir = cls._preextract_platform_libs(zip_importer)
            if ext_temp_dir:
                # 将临时目录加入 sys.path，使 Python 标准导入和 ctypes 都能找到模块
                if ext_temp_dir not in sys.path:
                    sys.path.insert(0, ext_temp_dir)
                # Windows: 加入 DLL 搜索路径
                if platform.system() == 'Windows' and hasattr(os, 'add_dll_directory'):
                    try:
                        token = os.add_dll_directory(ext_temp_dir)
                        dll_directory_tokens.append(token)
                    except OSError:
                        pass

        instance._plugin_importers[package_name] = PluginInfo(
            zip_importer=zip_importer,
            libs_prefix=libs_prefix,
            plugin_hash=plugin_hash,
            bundled_modules=bundled_modules,
            platform_modules=platform_modules,
            ext_temp_dir=ext_temp_dir,
            dll_directory_tokens=dll_directory_tokens,
        )
        if instance not in sys.meta_path:
            sys.meta_path.insert(0, instance)
        return libs_prefix

    @classmethod
    def _preextract_platform_libs(cls, zip_imp: zipimport.zipimporter) -> Optional[str]:
        """将 libs/ 和 libs/{platform}/ 下的所有文件预解压到临时目录，供 ctypes/原生加载使用。
        先解压 libs/ 下的通用文件，再覆盖 libs/{platform}/ 下的平台特定文件。"""
        current_platform = cls._current_platform
        platform_prefix = f"{current_platform}/"
        temp_dir = tempfile.mkdtemp(prefix='grmine_plat_')

        try:
            # 第一轮：解压 libs/ 下的通用文件（非平台特定）
            for file_path in zip_imp._files:
                normalized = file_path.replace('\\', '/')
                if not normalized.startswith('libs/'):
                    continue
                remaining = normalized[5:]
                if not remaining:
                    continue
                # 跳过平台特定目录下的文件（第二轮处理）
                for plat_key in ('win-amd64', 'win-x86', 'linux-x86_64', 'linux-aarch64', 'macos-x86_64', 'macos-arm64'):
                    if remaining.startswith(plat_key + '/'):
                        remaining = None
                        break
                if remaining is None:
                    continue

                lower = remaining.lower()
                if lower.endswith('.pyc') or lower.endswith('.pyo'):
                    continue
                if '/__pycache__/' in remaining:
                    continue

                try:
                    data = zip_imp.get_data(file_path)
                    parts = remaining.replace('/', os.sep)
                    dir_part = os.path.dirname(parts)
                    filename = os.path.basename(parts)

                    target_dir = os.path.join(temp_dir, dir_part) if dir_part else temp_dir
                    os.makedirs(target_dir, exist_ok=True)
                    target_path = os.path.join(target_dir, filename)
                    with open(target_path, 'wb') as f:
                        f.write(data)
                except Exception:
                    continue

            # 第二轮：解压 libs/{platform}/ 下的平台特定文件，覆盖通用文件
            for file_path in zip_imp._files:
                normalized = file_path.replace('\\', '/')
                if not normalized.startswith('libs/'):
                    continue
                remaining = normalized[5:]
                if not remaining.startswith(platform_prefix):
                    continue
                remaining = remaining[len(platform_prefix):]
                if not remaining:
                    continue

                lower = remaining.lower()
                if lower.endswith('.pyc') or lower.endswith('.pyo'):
                    continue
                if '/__pycache__/' in remaining:
                    continue

                try:
                    data = zip_imp.get_data(file_path)
                    parts = remaining.replace('/', os.sep)
                    dir_part = os.path.dirname(parts)
                    filename = os.path.basename(parts)
                    # 只对 .pyd/.so 文件去掉版本标签
                    name, ext = os.path.splitext(filename)
                    if ext.lower() in ('.pyd', '.so'):
                        filename = name.split('.')[0] + ext

                    target_dir = os.path.join(temp_dir, dir_part) if dir_part else temp_dir
                    os.makedirs(target_dir, exist_ok=True)
                    target_path = os.path.join(target_dir, filename)
                    with open(target_path, 'wb') as f:
                        f.write(data)
                except Exception:
                    continue
        except Exception:
            pass

        return temp_dir

    @staticmethod
    def _normalize_ext_filename_static(ext_filename: str) -> str:
        """静态方法版本：规范化扩展模块文件名。"""
        name, ext = os.path.splitext(ext_filename)
        if ext.lower() in ('.pyd', '.so'):
            base_name = name.split('.')[0]
            return base_name + ext
        return ext_filename

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
                if not remaining:
                    continue
                # 识别 Python 模块、扩展模块和 DLL 依赖
                lower_remaining = remaining.lower()
                is_python = remaining.endswith('.py')
                is_binary = lower_remaining.endswith('.pyd') or lower_remaining.endswith('.so')
                is_dep = lower_remaining.endswith('.dll') or lower_remaining.endswith('.dylib')
                if not (is_python or is_binary or is_dep):
                    continue
                # 对 Python 和扩展模块计算模块名
                if is_python or is_binary:
                    module_name = remaining.rsplit('.', 1)[0].replace('/', '.')
                    if module_name.endswith('.__init__'):
                        module_name = module_name[:-9]
                    parts = module_name.split('.')
                    # 去掉 CPython 版本标签部分 (如 .cp312-win_amd64)
                    if len(parts) > 1 and parts[-1].startswith('cp'):
                        module_name = '.'.join(parts[:-1])
                    if not module_name:
                        continue
                    for i in range(len(parts)):
                        bundled.add('.'.join(parts[:i + 1]))
                    if is_platform_specific and is_binary:
                        resolved = '.'.join(parts[:-1]) if len(parts) > 1 and parts[-1].startswith('cp') else module_name
                        if resolved not in platform_modules:
                            platform_modules[resolved] = file_path
                # DLL 依赖也需要注册到 bundled 集合中，以便验证
                if is_dep:
                    dep_name = remaining.rsplit('.', 1)[0].replace('/', '.')
                    if dep_name:
                        bundled.add(dep_name)
        except Exception:
            pass
        return bundled, platform_modules

    @classmethod
    def unregister_plugin(cls, package_name: str) -> bool:
        instance = cls.get_instance()
        if package_name not in instance._plugin_importers:
            return False
        plugin_info = instance._plugin_importers[package_name]

        # 清理预解压的临时目录
        if plugin_info.ext_temp_dir:
            if plugin_info.ext_temp_dir in sys.path:
                sys.path.remove(plugin_info.ext_temp_dir)
            # 关闭 DLL 搜索目录令牌
            for token in plugin_info.dll_directory_tokens:
                try:
                    token.close()
                except Exception:
                    pass
            # 清理临时目录
            try:
                import shutil
                shutil.rmtree(plugin_info.ext_temp_dir, ignore_errors=True)
            except Exception:
                pass

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
                # 如果有预解压目录且该模块在文件系统中存在，优先让标准 import 机制处理
                # 这样 importlib.resources 能正常工作
                if plugin_info.ext_temp_dir:
                    module_path = fullname.replace('.', os.sep)
                    pkg_init = os.path.join(plugin_info.ext_temp_dir, module_path, '__init__.py')
                    mod_file = os.path.join(plugin_info.ext_temp_dir, module_path + '.py')
                    if os.path.isfile(pkg_init) or os.path.isfile(mod_file):
                        return None  # 让标准 import 机制通过 sys.path 处理
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

        # 计算预解压目录中对应的真实路径
        plugin_info = self._plugin_importers.get(package_name)
        real_file_path = None
        if plugin_info and plugin_info.ext_temp_dir:
            module_rel_path = fullname.replace('.', os.sep)
            if is_package:
                real_file_path = os.path.join(plugin_info.ext_temp_dir, module_rel_path, '__init__.py')
            else:
                real_file_path = os.path.join(plugin_info.ext_temp_dir, module_rel_path + '.py')

        if is_extension and ext_binary is not None:
            # 扩展模块优先从预解压目录加载
            if plugin_info and plugin_info.ext_temp_dir:
                ext_suffix = '.pyd' if platform.system() == 'Windows' else '.so'
                ext_file = os.path.join(plugin_info.ext_temp_dir, module_rel_path + ext_suffix)
                if os.path.isfile(ext_file):
                    return self._load_extension_from_file(ext_file, fullname)
            return self._load_extension_module(ext_binary, file_path, fullname)

        return self._load_python_module(code_data, file_path, fullname, is_package, package_name, real_file_path)

    def _try_load_from_preextracted(self, ext_temp_dir: str, fullname: str, package_name: str):
        """尝试从预解压目录加载模块，使 __file__ 指向真实路径。"""
        module_path = fullname.replace('.', os.sep)

        # 尝试作为包加载
        init_path = os.path.join(ext_temp_dir, module_path, '__init__.py')
        if os.path.isfile(init_path):
            return self._load_python_file(init_path, fullname, is_package=True, package_name=package_name)

        # 尝试作为模块加载
        module_file = os.path.join(ext_temp_dir, module_path + '.py')
        if os.path.isfile(module_file):
            return self._load_python_file(module_file, fullname, is_package=False, package_name=package_name)

        # 尝试作为扩展模块加载
        ext_suffix = '.pyd' if platform.system() == 'Windows' else '.so'
        ext_file = os.path.join(ext_temp_dir, module_path + ext_suffix)
        if os.path.isfile(ext_file):
            return self._load_extension_from_file(ext_file, fullname)

        # 尝试隐式包（目录下有子模块但没有 __init__.py）
        pkg_dir = os.path.join(ext_temp_dir, module_path)
        if os.path.isdir(pkg_dir):
            # 检查是否有任何 .py 或 .pyd/.so 文件
            for entry in os.listdir(pkg_dir):
                if entry.endswith('.py') or entry.endswith(ext_suffix):
                    return self._load_python_file(None, fullname, is_package=True, package_name=package_name)

        return None

    def _load_python_file(self, file_path: Optional[str], fullname: str, is_package: bool, package_name: str):
        """从真实文件路径加载 Python 模块，__file__ 指向真实路径。"""
        module = types.ModuleType(fullname)
        if file_path:
            module.__file__ = file_path
        module.__loader__ = self
        module.__package__ = fullname if is_package else fullname.rpartition('.')[0]
        if is_package:
            module.__path__ = [os.path.dirname(file_path)] if file_path else []
        spec = importlib.machinery.ModuleSpec(
            fullname, self,
            origin=file_path,
            is_package=is_package,
        )
        if is_package:
            spec.submodule_search_locations = [os.path.dirname(file_path)] if file_path else []
        module.__spec__ = spec
        sys.modules[fullname] = module
        try:
            if file_path:
                with open(file_path, 'r', encoding='utf-8') as f:
                    code_data = f.read()
                code = compile(code_data, file_path, 'exec')
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

    def _load_extension_from_file(self, ext_path: str, fullname: str):
        """从真实文件路径加载扩展模块。"""
        spec = importlib.util.spec_from_file_location(fullname, ext_path)
        if spec and spec.loader:
            ext_module = importlib.util.module_from_spec(spec)
            sys.modules[fullname] = ext_module
            spec.loader.exec_module(ext_module)
            ext_module.__loader__ = self
            return ext_module
        raise ImportError(f"Cannot create spec for extension module {fullname} from {ext_path}")

    def _load_extension_module(self, ext_binary: bytes, file_path: str, fullname: str):
        plugin_name = self.get_current_plugin()
        plugin_info = self._plugin_importers.get(plugin_name) if plugin_name else None

        temp_dir = tempfile.mkdtemp(prefix='grmine_ext_')

        # 解压所有同平台的依赖 DLL/.so 到同一临时目录
        if plugin_info:
            self._extract_platform_deps(plugin_info, temp_dir)

        # 规范化扩展模块文件名：去掉 CPython 版本标签使其可被 importlib 识别
        ext_filename = os.path.basename(file_path.replace('\\', '/'))
        ext_filename = self._normalize_ext_filename(ext_filename, fullname)
        temp_ext_path = os.path.join(temp_dir, ext_filename)
        with open(temp_ext_path, 'wb') as f:
            f.write(ext_binary)

        # 将临时目录加入 DLL 搜索路径，使扩展模块能找到其 C++ 依赖
        _dll_search_dirs = []
        if platform.system() == 'Windows':
            if hasattr(os, 'add_dll_directory'):
                _dll_search_dirs.append(os.add_dll_directory(temp_dir))
            os.environ.setdefault('PATH', '')
            old_path = os.environ['PATH']
            os.environ['PATH'] = temp_dir + os.pathsep + old_path
        else:
            old_ld = os.environ.get('LD_LIBRARY_PATH', '')
            os.environ['LD_LIBRARY_PATH'] = temp_dir + os.pathsep + old_ld

        try:
            spec = importlib.util.spec_from_file_location(fullname, temp_ext_path)
            if spec and spec.loader:
                ext_module = importlib.util.module_from_spec(spec)
                sys.modules[fullname] = ext_module
                spec.loader.exec_module(ext_module)
                ext_module.__file__ = "<zip>" + file_path
                ext_module.__loader__ = self
                return ext_module
            raise ImportError(f"Cannot create spec for extension module {fullname}")
        finally:
            # 还原环境变量（add_dll_directory 返回的令牌自动失效）
            if platform.system() == 'Windows':
                os.environ['PATH'] = old_path
                for d in _dll_search_dirs:
                    try:
                        d.close()
                    except Exception:
                        pass
            else:
                os.environ['LD_LIBRARY_PATH'] = old_ld

    def _normalize_ext_filename(self, ext_filename: str, fullname: str) -> str:
        """将含 CPython 版本标签的文件名规范化为 importlib 可识别的名称。
        例如: _curl.cp312-win_amd64.pyd -> _curl.pyd
        """
        name, ext = os.path.splitext(ext_filename)
        if ext.lower() in ('.pyd', '.so'):
            # 去掉 .so 文件中的版本后缀如 .cpython-312-x86_64-linux-gnu
            # 以及 .pyd 中的 .cp312-win_amd64
            base_name = name.split('.')[0]
            return base_name + ext
        return ext_filename

    def _extract_platform_deps(self, plugin_info: 'PluginInfo', temp_dir: str) -> None:
        """将插件中同平台的所有 .dll/.so 依赖解压到临时目录。"""
        current_platform = self._current_platform
        platform_prefix = f"{current_platform}/"
        zip_imp = plugin_info.zip_importer

        try:
            for file_path in zip_imp._files:
                normalized = file_path.replace('\\', '/')
                if not normalized.startswith('libs/'):
                    continue
                remaining = normalized[5:]

                # 只处理当前平台的文件
                is_platform_specific = remaining.startswith(platform_prefix)
                if is_platform_specific:
                    remaining = remaining[len(platform_prefix):]
                else:
                    continue

                lower = remaining.lower()
                # 提取 .dll, .pyd, .so 等二进制依赖
                is_dep = (
                    lower.endswith('.dll') or
                    lower.endswith('.pyd') or
                    lower.endswith('.so') or
                    lower.endswith('.dylib')
                )
                if not is_dep:
                    continue

                try:
                    data = zip_imp.get_data(file_path)
                    dep_filename = os.path.basename(remaining)
                    dep_path = os.path.join(temp_dir, dep_filename)
                    if not os.path.exists(dep_path):
                        with open(dep_path, 'wb') as f:
                            f.write(data)
                except Exception:
                    pass
        except Exception:
            pass

    def _load_python_module(self, code_data: bytes, file_path: str, fullname: str, is_package: bool, package_name: str, real_file_path: Optional[str] = None):
        module = types.ModuleType(fullname)
        # 优先使用真实文件路径，使 os.path.dirname(__file__) 能找到同目录的 .pyd
        if real_file_path and os.path.isfile(real_file_path):
            module.__file__ = real_file_path
        else:
            module.__file__ = "<zip>" + file_path
        module.__loader__ = self
        module.__package__ = fullname if is_package else fullname.rpartition('.')[0]
        if is_package:
            # __path__ 需要同时包含 zip 路径和文件系统路径：
            # - zip 路径使 importlib.resources 能找到包内资源文件
            # - 文件系统路径使 os.path.dirname(__file__) 能找到同目录的 .pyd
            plugin_info = self._plugin_importers.get(package_name)
            path_entries = []
            # 添加 zip 路径（用于 importlib.resources 访问包内资源）
            zip_imp = plugin_info.zip_importer if plugin_info else None
            if zip_imp:
                module_rel = fullname.replace('.', '/')
                zip_path = f"{zip_imp.archive}{os.sep}libs{os.sep}{module_rel}"
                path_entries.append(zip_path)
            # 添加文件系统路径（用于 C++ 扩展的 DLL 查找）
            if real_file_path:
                fs_dir = os.path.dirname(real_file_path)
                if fs_dir not in path_entries:
                    path_entries.append(fs_dir)
            module.__path__ = path_entries
        spec = importlib.machinery.ModuleSpec(
            fullname, self,
            origin=module.__file__,
            is_package=is_package,
        )
        if is_package:
            spec.submodule_search_locations = module.__path__
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
