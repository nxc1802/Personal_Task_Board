"""Acquisition Adapter Base Interface.

Định nghĩa hợp đồng chuẩn (Contract) cho toàn bộ các source adapters trong hệ thống
Personal Task Board v1 theo docs/v1.md.
"""

from abc import ABC, abstractmethod
from datetime import datetime
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from ptb_contracts import IngestionCheckpointRecord, RawEventRecord, SourceType

logger = logging.getLogger("ptb.acquisition.adapters.base")


class AcquisitionAdapter(ABC):
    """Hợp đồng trừu tượng cơ sở cho toàn bộ Acquisition Adapters."""

    def __init__(self, source_type: SourceType, tenant_id: str = "local-user"):
        self.source_type = source_type
        self.tenant_id = tenant_id

    @abstractmethod
    async def discover(self) -> List[Dict[str, Any]]:
        """Liệt kê các streams, channels, workspaces hoặc project keys có sẵn tại nguồn."""
        pass

    @abstractmethod
    async def backfill(
        self, stream_id: str, since: Optional[datetime] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập dữ liệu lịch sử/quá khứ từ stream_id chỉ định."""
        pass

    @abstractmethod
    async def poll_incremental(
        self, stream_id: str, checkpoint: Optional[IngestionCheckpointRecord] = None
    ) -> AsyncIterator[RawEventRecord]:
        """Thu thập dữ liệu mới phát sinh kể từ checkpoint."""
        pass

    @abstractmethod
    async def health(self) -> Dict[str, Any]:
        """Trả về trạng thái hoạt động của adapter (healthy, error, path_found,...)."""
        pass

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(source_type={self.source_type}, tenant_id={self.tenant_id})>"
