import pytest


@pytest.fixture(autouse=True)
def isolate_deployment_credentials(tmp_path, monkeypatch):
    """Keep local deployment credentials and image markers outside test runs."""
    monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "test-deployment-pool"))
