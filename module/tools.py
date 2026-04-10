import time
import json
import sys
from typing import Optional
from enum import Enum

try:
    import colorama
    colorama.init()
    HAS_COLORAMA = True
except ImportError:
    HAS_COLORAMA = False


class LogLevel(Enum):
    DEBUG = 0
    INFO = 1
    WARNING = 2
    ERROR = 3
    CRITICAL = 4


class Console:
    _global_log_level: LogLevel = LogLevel.INFO
    _log_file: Optional[str] = None
    
    def __init__(self, name: str, log_level: Optional[LogLevel] = None):
        self.name = name
        self._log_level = log_level

    @classmethod
    def set_global_log_level(cls, level: LogLevel) -> None:
        cls._global_log_level = level

    @classmethod
    def set_log_file(cls, file_path: str) -> None:
        cls._log_file = file_path

    @property
    def log_level(self) -> LogLevel:
        return self._log_level if self._log_level is not None else self._global_log_level

    def get_time(self) -> str:
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())

    def _colorize(self, text: str, color: str) -> str:
        if not HAS_COLORAMA:
            return text
        
        color_map = {
            'white': colorama.Fore.WHITE,
            'yellow': colorama.Fore.YELLOW,
            'red': colorama.Fore.RED,
            'green': colorama.Fore.GREEN,
            'cyan': colorama.Fore.CYAN,
            'magenta': colorama.Fore.MAGENTA,
        }
        
        color_code = color_map.get(color.lower(), colorama.Fore.WHITE)
        return f"{color_code}{text}{colorama.Style.RESET_ALL}"

    def _write_log(self, level: str, message: str) -> None:
        if self._log_file:
            try:
                with open(self._log_file, "a", encoding="utf-8") as f:
                    f.write(f"{self.get_time()} [{level}][{self.name}] {message}\n")
            except Exception:
                pass

    def _should_log(self, level: LogLevel) -> bool:
        return level.value >= self.log_level.value

    def debug(self, *args, **kwargs) -> None:
        if not self._should_log(LogLevel.DEBUG):
            return
        message = " ".join(str(arg) for arg in args)
        formatted = f"{self.get_time()} [DEBUG][{self.name}] {message}"
        print(self._colorize(formatted, 'cyan'), **kwargs)
        self._write_log("DEBUG", message)

    def info(self, *args, **kwargs) -> None:
        if not self._should_log(LogLevel.INFO):
            return
        message = " ".join(str(arg) for arg in args)
        formatted = f"{self.get_time()} [INFO][{self.name}] {message}"
        print(self._colorize(formatted, 'white'), **kwargs)
        self._write_log("INFO", message)

    def warning(self, *args, **kwargs) -> None:
        if not self._should_log(LogLevel.WARNING):
            return
        message = " ".join(str(arg) for arg in args)
        formatted = f"{self.get_time()} [WARN][{self.name}] {message}"
        print(self._colorize(formatted, 'yellow'), **kwargs)
        self._write_log("WARN", message)

    def error(self, *args, **kwargs) -> None:
        if not self._should_log(LogLevel.ERROR):
            return
        message = " ".join(str(arg) for arg in args)
        formatted = f"{self.get_time()} [ERROR][{self.name}] {message}"
        print(self._colorize(formatted, 'red'), **kwargs)
        self._write_log("ERROR", message)

    def critical(self, *args, **kwargs) -> None:
        if not self._should_log(LogLevel.CRITICAL):
            return
        message = " ".join(str(arg) for arg in args)
        formatted = f"{self.get_time()} [CRITICAL][{self.name}] {message}"
        print(self._colorize(formatted, 'magenta'), **kwargs)
        self._write_log("CRITICAL", message)

    def success(self, *args, **kwargs) -> None:
        if not self._should_log(LogLevel.INFO):
            return
        message = " ".join(str(arg) for arg in args)
        formatted = f"{self.get_time()} [SUCCESS][{self.name}] {message}"
        print(self._colorize(formatted, 'green'), **kwargs)
        self._write_log("SUCCESS", message)


def read_config(config_file: str) -> dict:
    with open(config_file, "r", encoding="utf-8") as f:
        return json.load(f)


def write_config(config_file: str, config: dict, indent: int = 2) -> None:
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=indent, ensure_ascii=False)


def ensure_dir(path: str) -> None:
    import os
    if not os.path.exists(path):
        os.makedirs(path)
