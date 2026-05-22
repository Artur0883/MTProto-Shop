import os

PORT = int(os.getenv("PROXY_PORT", "443"))

USERS = {}

MODES = {
    "classic": False,
    "secure": True,
    "tls": False,
}

TLS_DOMAIN = os.getenv("TLS_DOMAIN", "www.google.com")
