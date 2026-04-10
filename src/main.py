import fastapi
from module.GrAPI import GrAPI

API = GrAPI(__doc__)


def main():
    try:
        API.console.info(f"Plugin {API.get_plugin_name()} v{API.get_plugin_version()} loading...")
        
        dep_plugin = API.get_plugin("grmine.a+b")
        API.console.info(f"Got dependency plugin: {dep_plugin}")
        
        if dep_plugin and "plugin" in dep_plugin:
            result = dep_plugin["plugin"].apb(1, 1)
            API.console.info(f"apb(1, 1) = {result}")
        
        API.app.test()
    except Exception as e:
        API.console.error(f"Error in main: {e}")


def loaded():
    API.console.success(f"Plugin {API.get_plugin_name()} initialized successfully!")
