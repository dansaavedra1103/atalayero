from pathlib import Path

import yaml

COMPOSE = Path(__file__).parents[2] / "docker-compose.yml"


def test_every_published_port_listens_on_localhost_only() -> None:
    """Ollama and Redpanda take no credentials, and the rest sits behind its own: nothing is
    published beyond this machine."""
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    published = {name: s.get("ports", []) for name, s in services.items()}
    assert any(published.values())
    for name, ports in published.items():
        for port in ports:
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} on every interface"
