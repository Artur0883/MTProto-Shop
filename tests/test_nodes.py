import importlib
import json
import os


def _reload_with(env: dict):
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import config
    importlib.reload(config)
    import nodes
    importlib.reload(nodes)
    return nodes


def test_single_node_fallback_when_unset():
    nodes = _reload_with(
        {
            "PROXY_NODES": None,
            "SERVER_HOST": "1.2.3.4",
            "TELEMT_API_URL": "http://mtproto:9091",
        }
    )
    ns = nodes.get_nodes()
    assert len(ns) == 1
    assert ns[0].public_host == "1.2.3.4"
    assert ns[0].api_url == "http://mtproto:9091"
    assert ns[0].primary is True


def test_multi_node_from_json():
    payload = json.dumps(
        [
            {"name": "A", "public_host": "1.1.1.1", "api_url": "http://mtproto:9091", "primary": True},
            {"name": "B", "public_host": "2.2.2.2", "api_url": "http://100.87.48.113:9091", "primary": False},
        ]
    )
    nodes = _reload_with({"PROXY_NODES": payload, "SERVER_HOST": "1.1.1.1"})
    ns = nodes.get_nodes()
    assert [n.name for n in ns] == ["A", "B"]
    assert nodes.primary_node(ns).name == "A"
    assert ns[1].api_url == "http://100.87.48.113:9091"
