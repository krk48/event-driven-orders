import os


def setting(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def required_setting(name: str) -> str:
    value = setting(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value
