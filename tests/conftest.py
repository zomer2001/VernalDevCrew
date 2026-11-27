import pytest
from pathlib import Path
import sys

# Test this checkout even when its virtualenv is shared with a sibling worktree.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolate_deployment_credentials(tmp_path, monkeypatch):
    """Keep local deployment credentials and image markers outside test runs."""
    monkeypatch.setenv("VERNAL_POOL_DIR", str(tmp_path / "test-deployment-pool"))
    monkeypatch.setenv("VERNAL_SERVICE_TOKEN_FILE", str(tmp_path / "test-deployment.token"))
