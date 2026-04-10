import os
import sys
import json
import shutil
import argparse
import tempfile
import subprocess
import platform
from pathlib import Path
from typing import Dict, List, Optional, Set


PLATFORMS = {
    'win-amd64': ['win_amd64', 'win32'],
    'win-x86': ['win32'],
    'linux-x86_64': ['manylinux', 'linux'],
    'linux-aarch64': ['manylinux_aarch64', 'linux_aarch64'],
    'macos-x86_64': ['macosx', 'darwin'],
    'macos-arm64': ['macosx_arm64', 'darwin_arm64'],
}

DEFAULT_PLATFORMS = ['win-amd64', 'linux-x86_64', 'macos-x86_64']


class PluginBuilder:
    def __init__(self, source_dir: str, output_dir: Optional[str] = None):
        self.source_dir = Path(source_dir).resolve()
        self.output_dir = Path(output_dir).resolve() if output_dir else self.source_dir.parent
        self.info_file = self.source_dir / "info.json"
        self.info: Dict = {}
        
    def validate_source(self) -> bool:
        if not self.source_dir.exists():
            print(f"Error: Source directory '{self.source_dir}' does not exist")
            return False
        
        if not self.info_file.exists():
            print(f"Error: info.json not found in '{self.source_dir}'")
            return False
        
        try:
            with open(self.info_file, "r", encoding="utf-8") as f:
                self.info = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in info.json: {e}")
            return False
        
        required_fields = ["package_name", "plugin_name", "entrance"]
        for field in required_fields:
            if field not in self.info:
                print(f"Error: Missing required field '{field}' in info.json")
                return False
        
        entrance = self.info["entrance"]
        entrance_file = self.source_dir / f"{entrance}.py"
        if not entrance_file.exists():
            print(f"Error: Entrance file '{entrance}.py' not found")
            return False
        
        return True
    
    def download_dependencies(self, libs_dir: Path, target_platforms: Optional[List[str]] = None) -> bool:
        modules = self.info.get("modules", [])
        bundled_modules = [m for m in modules if isinstance(m, dict) and m.get("bundled", False)]
        
        if not bundled_modules:
            print("No bundled dependencies to download")
            return True
        
        platforms_to_download = target_platforms or self._get_default_platforms()
        print(f"Downloading {len(bundled_modules)} dependencies for platforms: {platforms_to_download}")
        
        all_success = True
        for module_info in bundled_modules:
            module_name = module_info.get("module_name") or module_info.get("import_name")
            version = module_info.get("version")
            
            if not module_name:
                print(f"Warning: Skipping module with no name: {module_info}")
                all_success = False
                continue
            
            package_spec = f"{module_name}=={version}" if version else module_name
            print(f"\n  Processing {package_spec}...")
            
            if not self._download_for_platforms(module_name, package_spec, libs_dir, platforms_to_download):
                all_success = False
        
        return all_success
    
    def _get_default_platforms(self) -> List[str]:
        current = platform.system().lower()
        machine = platform.machine().lower()
        
        if current == 'windows':
            return ['win-amd64']
        elif current == 'darwin':
            if machine in ['arm64', 'aarch64']:
                return ['macos-arm64']
            return ['macos-x86_64']
        else:
            if machine in ['aarch64', 'arm64']:
                return ['linux-aarch64']
            return ['linux-x86_64']
    
    def _download_for_platforms(self, module_name: str, package_spec: str, libs_dir: Path, target_platforms: List[str]) -> bool:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            print(f"    Downloading dependencies...")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "download", 
                 "--dest", str(temp_path), package_spec],
                capture_output=True, text=True, timeout=300
            )
            
            if result.returncode != 0:
                print(f"      Error downloading {package_spec}: {result.stderr.strip()}")
                return False
            
            wheel_files = list(temp_path.glob("*.whl"))
            source_files = list(temp_path.glob("*.tar.gz")) + list(temp_path.glob("*.zip"))
            
            if not wheel_files and not source_files:
                print(f"      Warning: No distributable files found for {package_spec}")
                return False
            
            success = False
            for wheel_file in wheel_files:
                self._extract_wheel(wheel_file, libs_dir, module_name, target_platforms)
                success = True
            
            for source_file in source_files:
                self._extract_source(source_file, libs_dir, module_name)
                success = True
            
            return success
    
    def _extract_wheel(self, wheel_path: Path, libs_dir: Path, module_name: str, target_platforms: List[str]) -> None:
        import zipfile
        
        wheel_name = wheel_path.name
        
        is_platform_specific = any(tag in wheel_name for tag in ['win_', 'linux_', 'macosx', 'manylinux'])
        has_extension = any(tag in wheel_name for tag in ['win_', 'linux_', 'macosx', 'manylinux']) and \
                       not wheel_name.endswith('none-any.whl')
        
        print(f"      Extracting {wheel_name}...")
        
        extracted_files: Set[str] = set()
        
        with zipfile.ZipFile(wheel_path, 'r') as zf:
            for member in zf.namelist():
                if member.endswith('/'):
                    continue
                
                if member.endswith('.pyc') or member.endswith('.pyo'):
                    continue
                
                if '.dist-info/' in member or '-info/' in member:
                    continue
                
                if member in extracted_files:
                    continue
                
                member_lower = member.lower()
                is_binary = member_lower.endswith('.pyd') or member_lower.endswith('.so')
                
                if has_extension and is_binary:
                    for target_platform in target_platforms:
                        platform_libs_dir = libs_dir / target_platform
                        target_path = platform_libs_dir / member
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src, open(target_path, 'wb') as dst:
                            dst.write(src.read())
                        extracted_files.add(f"{target_platform}/{member}")
                else:
                    target_path = libs_dir / member
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(target_path, 'wb') as dst:
                        dst.write(src.read())
                    extracted_files.add(member)
        
        print(f"      Bundled: {module_name}")
    
    def _extract_source(self, source_path: Path, libs_dir: Path, module_name: str) -> None:
        import tarfile
        import zipfile
        
        source_name = source_path.name
        print(f"      Extracting {source_name}...")
        
        extracted_files: Set[str] = set()
        
        if source_name.endswith('.tar.gz') or source_name.endswith('.tgz'):
            with tarfile.open(source_path, 'r:gz') as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    if member.name.endswith('.pyc') or member.name.endswith('.pyo'):
                        continue
                    if '.dist-info/' in member or '-info/' in member or '.egg-info/' in member:
                        continue
                    parts = member.name.split('/')
                    if len(parts) <= 1:
                        continue
                    stripped = '/'.join(parts[1:])
                    if stripped.endswith('/'):
                        continue
                    if stripped in extracted_files:
                        continue
                    target_path = libs_dir / stripped
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with tf.extractfile(member) as src:
                        if src:
                            with open(target_path, 'wb') as dst:
                                dst.write(src.read())
                    extracted_files.add(stripped)
        elif source_name.endswith('.zip'):
            with zipfile.ZipFile(source_path, 'r') as zf:
                for member in zf.namelist():
                    if member.endswith('/'):
                        continue
                    if member.endswith('.pyc') or member.endswith('.pyo'):
                        continue
                    if '.dist-info/' in member or '-info/' in member or '.egg-info/' in member:
                        continue
                    parts = member.split('/')
                    if len(parts) <= 1:
                        continue
                    stripped = '/'.join(parts[1:])
                    if stripped in extracted_files:
                        continue
                    target_path = libs_dir / stripped
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src:
                        with open(target_path, 'wb') as dst:
                            dst.write(src.read())
                    extracted_files.add(stripped)
        
        print(f"      Bundled (source): {module_name}")
    
    def _validate_bundled_deps(self, libs_dir: Path) -> bool:
        modules = self.info.get("modules", [])
        bundled_modules = [m for m in modules if isinstance(m, dict) and m.get("bundled", False)]
        
        if not bundled_modules:
            return True
        
        all_valid = True
        for module_info in bundled_modules:
            import_name = module_info.get("import_name", "")
            if not import_name:
                continue
            
            module_path = import_name.replace('.', os.sep)
            found = (
                (libs_dir / module_path / "__init__.py").exists() or
                (libs_dir / f"{module_path}.py").exists() or
                any((libs_dir / module_path).parent.glob(f"{module_path.split(os.sep)[-1]}*"))
            )
            
            if not found:
                platform_dir = libs_dir / self._get_default_platforms()[0] if self._get_default_platforms() else None
                if platform_dir:
                    found = (
                        (platform_dir / module_path / "__init__.py").exists() or
                        (platform_dir / f"{module_path}.py").exists()
                    )
            
            if not found:
                print(f"      ERROR: Bundled module '{import_name}' not found in libs/ after extraction!")
                all_valid = False
            else:
                print(f"      OK: Bundled module '{import_name}' verified")
        
        return all_valid
    
    def build(self, include_libs: bool = True, target_platforms: Optional[List[str]] = None) -> Optional[Path]:
        if not self.validate_source():
            return None
        
        plugin_name = self.info["plugin_name"]
        version = self.info.get("version", "1.0.0")
        output_filename = f"{plugin_name} {version}.grpl"
        output_path = self.output_dir / output_filename
        
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            for item in self.source_dir.iterdir():
                if item.name in ['__pycache__', '.git', '.gitignore', '*.pyc']:
                    continue
                
                dest = temp_path / item.name
                if item.is_file():
                    shutil.copy2(item, dest)
                else:
                    shutil.copytree(item, dest)
            
            if include_libs:
                libs_dir = temp_path / "libs"
                libs_dir.mkdir(exist_ok=True)
                deps_ok = self.download_dependencies(libs_dir, target_platforms)
                if not deps_ok:
                    print("\nError: Failed to download some bundled dependencies!")
                    print("  The plugin will be built WITHOUT bundled libs.")
                    print("  The loader will fall back to pip install at runtime.")
                
                if not self._validate_bundled_deps(libs_dir):
                    print("\nWarning: Some bundled modules are missing from libs/ directory!")
                    print("  The loader will fall back to pip install at runtime.")
            
            import zipfile
            with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(temp_path):
                    dirs[:] = [d for d in dirs if d not in ['__pycache__', '.git']]
                    
                    for file in files:
                        if file.endswith('.pyc') or file.endswith('.pyo'):
                            continue
                        
                        file_path = Path(root) / file
                        arc_name = file_path.relative_to(temp_path)
                        zf.write(file_path, arc_name)
        
        print(f"\nPlugin built successfully: {output_path}")
        print(f"  Package: {self.info['package_name']}")
        print(f"  Name: {plugin_name}")
        print(f"  Version: {version}")
        
        return output_path
    
    def create_template(self) -> None:
        self.source_dir.mkdir(parents=True, exist_ok=True)
        
        info_template = {
            "package_name": "com.example.myplugin",
            "plugin_name": "MyPlugin",
            "plugin_info": "A sample GrMine plugin",
            "entrance": "main",
            "author": "Your Name",
            "version": "1.0.0",
            "author_info": "Email: your@email.com",
            "grapi_version": "2.0",
            "dependent_plugin": [],
            "modules": [
                {
                    "import_name": "requests",
                    "module_name": "requests",
                    "version": "2.31.0",
                    "bundled": False
                }
            ],
            "python_version": ">=3.8"
        }
        
        with open(self.info_file, "w", encoding="utf-8") as f:
            json.dump(info_template, f, indent=2, ensure_ascii=False)
        
        main_template = '''from module.GrAPI import GrAPI

API = GrAPI(__doc__)


def main():
    API.console.info(f"Plugin {API.get_plugin_name()} loaded!")


def loaded():
    API.console.success("Plugin initialization complete!")
'''
        
        main_file = self.source_dir / "main.py"
        with open(main_file, "w", encoding="utf-8") as f:
            f.write(main_template)
        
        print(f"Plugin template created in: {self.source_dir}")
        print("  - info.json (plugin metadata)")
        print("  - main.py (plugin entry point)")


def main():
    parser = argparse.ArgumentParser(
        description="GrMine Plugin Builder Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Build a plugin from source directory
  python build_plugin.py build ./src
  
  # Build without bundling dependencies
  python build_plugin.py build ./src --no-libs
  
  # Build for all platforms
  python build_plugin.py build ./src --platforms all
  
  # Build for specific platforms
  python build_plugin.py build ./src --platforms win-amd64,linux-x86_64
  
  # Create a new plugin template
  python build_plugin.py init ./my_plugin

Available platforms:
  win-amd64      - Windows 64-bit
  win-x86        - Windows 32-bit
  linux-x86_64   - Linux 64-bit
  linux-aarch64  - Linux ARM64
  macos-x86_64   - macOS Intel
  macos-arm64    - macOS Apple Silicon
"""
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    build_parser = subparsers.add_parser("build", help="Build a plugin package")
    build_parser.add_argument("source", help="Source directory containing plugin files")
    build_parser.add_argument("-o", "--output", help="Output directory for the .grpl file")
    build_parser.add_argument("--no-libs", action="store_true", 
                              help="Don't bundle dependencies")
    build_parser.add_argument("--platforms", type=str, default=None,
                              help="Target platforms (comma-separated, or 'all' for all platforms)")
    
    init_parser = subparsers.add_parser("init", help="Create a new plugin template")
    init_parser.add_argument("directory", help="Directory for the new plugin")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    if args.command == "build":
        target_platforms = None
        if args.platforms:
            if args.platforms.lower() == 'all':
                target_platforms = list(PLATFORMS.keys())
            else:
                target_platforms = [p.strip() for p in args.platforms.split(',')]
        
        builder = PluginBuilder(args.source, args.output)
        builder.build(include_libs=not args.no_libs, target_platforms=target_platforms)
    
    elif args.command == "init":
        builder = PluginBuilder(args.directory)
        builder.create_template()


if __name__ == "__main__":
    main()
