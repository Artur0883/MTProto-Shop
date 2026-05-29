import importlib


def _settings_with(monkeypatch, **env):
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(value))
    import config

    importlib.reload(config)
    return config.get_settings()


def test_telemt_internal_proxy_port_defaults_to_container_port(monkeypatch):
    settings = _settings_with(monkeypatch, TELEMT_PROXY_INTERNAL_PORT=None)

    assert settings.telemt_proxy_internal_port == 443


def test_telemt_internal_proxy_port_is_separate_from_public_port(monkeypatch):
    settings = _settings_with(
        monkeypatch,
        PROXY_PORT=8443,
        TELEMT_PROXY_INTERNAL_PORT=443,
    )

    assert settings.proxy_port == 8443
    assert settings.telemt_proxy_internal_port == 443
