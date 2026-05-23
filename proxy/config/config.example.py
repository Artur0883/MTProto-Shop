import os

PORT = int(os.getenv("PROXY_PORT", "443"))

USERS = {}

MODES = {
    "classic": False,
    "secure": True,
    "tls": True,
}

TLS_DOMAIN = os.getenv("TLS_DOMAIN", "www.cloudflare.com")
