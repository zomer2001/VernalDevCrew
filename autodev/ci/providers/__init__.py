"""CI Provider 工厂与导出（WP-3）。"""

from autodev.ci.providers.base import CIHandle, CIProvider, CIStatus
from autodev.ci.providers.external import GitLabCIProvider, JenkinsCIProvider, WebhookCIProvider
from autodev.ci.providers.gitea import GiteaCIProvider

__all__ = [
    "CIHandle",
    "CIProvider",
    "CIStatus",
    "GitLabCIProvider",
    "GiteaCIProvider",
    "JenkinsCIProvider",
    "WebhookCIProvider",
]
