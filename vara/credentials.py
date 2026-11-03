import keyring
from keyring.errors import KeyringError

# API keys live in the OS credential store (Windows Credential Manager, macOS Keychain,
# Linux Secret Service) under this service name, one entry per provider.
SERVICE = "vara"


def get_api_key(provider: str) -> str | None:
    try:
        return keyring.get_password(SERVICE, provider)
    except KeyringError:
        return None
