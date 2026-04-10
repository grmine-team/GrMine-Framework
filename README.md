# GrMine Framework

> A Python-based plugin framework inspired by Minecraft Spigot — with plugin packaging, dependency management, cross-platform bundled libraries, and a clean developer API.

## Features

- **Plugin Packaging** — Plugins are distributed as `.grpl` files (ZIP format) containing source code, metadata (`info.json`), and optionally bundled third-party libraries.
- **Custom Import System** — Built-in `zipimport` implementation that loads Python modules directly from ZIP archives without extraction.
- **Dependency Management**
  - **Inter-plugin dependencies**: Declare dependencies between plugins; the framework resolves load order automatically via topological sort.
  - **Third-party dependencies**: Specify pip packages in `info.json`; missing ones are auto-installed at runtime.
- **Bundled Libraries** — Ship platform-specific binary extensions (`.pyd` / `.so`) inside plugins. No need for users to pre-install dependencies.
- **Cross-Platform** — Supports Windows (amd64), macOS (x86_64 / arm64), and Linux (x86_64 / aarch64).
- **Plugin Lifecycle** — Hooks: `main()` (entry point), `loaded()` (post-load callback), `unload()`.
- **GrAPI SDK** — Clean API for plugins: config read/write, data file access, bundled module import, inter-plugin communication.

## Project Structure

```
GrMine Framework/
├── main.py                 # Entry point — initializes and loads all plugins
├── plugin.py               # Plugin manager — discovery, dependency resolution, lifecycle
├── config.json             # Global configuration
├── requirements.txt        # Python dependencies
├── module/
│   ├── GrAPI.py            # GrAPI class — plugin-facing SDK
│   ├── plugin_importer.py  # Custom meta-path finder/loader for bundled libs
│   ├── tools.py            # Console logger utility
│   └── zipimport.py        # Custom zipimport implementation
├── tools/
│   └── build_plugin.py     # CLI tool to build .grpl plugin packages
├── src/                    # Example plugin source directory
│   ├── info.json           # Plugin metadata
│   └── main.py             # Plugin entry point
└── plugins/                # Directory where .grpl files are placed
```

## Quick Start

### Prerequisites

- Python >= 3.8
- pip

### Install Dependencies

```bash
pip install -r requirements.txt
```

### Run

```bash
python main.py
```

Place your `.grpl` plugin files into the `plugins/` directory before running.

## Developing a Plugin

### 1. Create a Plugin Project

Use the built-in tool to scaffold a new plugin:

```bash
python tools/build_plugin.py init ./my_plugin
```

This creates:
```
my_plugin/
├── info.json    # Plugin metadata
└── main.py      # Entry point
```

### 2. Configure `info.json`

```json
{
  "package_name": "com.example.myplugin",
  "plugin_name": "MyPlugin",
  "plugin_info": "A short description of your plugin",
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
      "bundled": false
    }
  ],
  "python_version": ">=3.8"
}
```

| Field | Description |
|---|---|
| `package_name` | Unique identifier (used as internal key) |
| `plugin_name` | Display name |
| `entrance` | Entry point filename (without `.py`) |
| `dependent_plugin` | List of `package_name`s this plugin depends on |
| `modules` | Third-party dependencies; set `"bundled": true` to ship inside `.grpl` |

### 3. Write Your Plugin Code

```python
from module.GrAPI import GrAPI

API = GrAPI(__doc__)


def main():
    API.console.info(f"Plugin {API.get_plugin_name()} loaded!")
    
    # Read/write config
    config = API.read_config("settings.json")
    API.write_config("settings.json", {"key": "value"})
    
    # Access data files from the plugin archive
    data = API.get_data_text("data.txt")
    
    # Get a dependent plugin
    dep = API.get_plugin("com.example.other")
    
    # Import a bundled library
    import fastapi


def loaded():
    API.console.success("Plugin initialization complete!")


def unload():
    API.console.info("Cleaning up...")
```

### 4. Build the Plugin

```bash
# Build with bundled dependencies (default)
python tools/build_plugin.py build ./my_plugin

# Build without bundled libs (will use pip at runtime)
python tools/build_plugin.py build ./my_plugin --no-libs

# Build for specific platforms
python tools/build_plugin.py build ./my_plugin --platforms win-amd64,linux-x86_64

# Build for all platforms
python tools/build_plugin.py build ./my_plugin --platforms all
```

Output: `{plugin_name} {version}.grpl` — drop this file into the `plugins/` directory.

## Architecture Overview

### Plugin Loading Flow

```
main.py → Plugin.get_plugins()
         → Scan plugins/ directory for .grpl files
         → Parse info.json → resolve dependencies (topological sort)
         → Register with PluginImporter (custom meta path finder)
         → Load entry point module via zipimport
         → Call main() / loaded() hooks
```

### Bundled Library Import System

When a plugin declares `"bundled": true` for a dependency:

1. At **build time**, `build_plugin.py` downloads wheels/sources via pip and extracts them into `libs/{platform}/` inside the `.grpl`.
2. At **load time**, `plugin_importer.py` registers a custom `MetaPathFinder` that intercepts imports under `grapi.plugin_libs.{hash}.*`.
3. When the plugin does `import fastapi`, the finder reads the module source/binary directly from the ZIP archive — no filesystem extraction needed.
4. Platform-specific extensions (`.pyd` / `.so`) are matched to the current OS/architecture.

## License

MIT
