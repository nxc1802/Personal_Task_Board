"""Neo4j Client helper configured for Neo4j AuraDB and self-hosted instances.

Hỗ trợ giao thức bảo mật neo4j+s:// của AuraDB, quản lý connection pool
và thực thi các ràng buộc schema / constraints tự động.
"""

import os
import re
import logging
from pathlib import Path
from typing import Any, List, Optional
from neo4j import AsyncGraphDatabase, AsyncDriver

logger = logging.getLogger("ptb.database.neo4j_client")

DEFAULT_CONSTRAINTS_FILE = Path(__file__).parent.parent.parent / "neo4j" / "constraints" / "001_constraints.cypher"


class Neo4jClient:
    def __init__(
        self,
        uri: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
    ):
        self.uri = uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.username = username or os.getenv("NEO4J_USERNAME", "neo4j")
        self.password = password or os.getenv("NEO4J_PASSWORD", "personal_task_board_secret_2026")
        self.database = database or os.getenv("NEO4J_DATABASE", "neo4j")
        self._driver: Optional[AsyncDriver] = None

    def get_driver(self) -> AsyncDriver:
        """Khởi tạo AsyncDriver tối ưu hóa cho AuraDB (neo4j+s://)."""
        if self._driver is None:
            # AuraDB khuyến nghị max_connection_lifetime và keep_alive
            self._driver = AsyncGraphDatabase.driver(
                self.uri,
                auth=(self.username, self.password),
                max_connection_lifetime=30 * 60,
                max_connection_pool_size=50,
                connection_acquisition_timeout=60,
            )
        return self._driver

    async def verify_connectivity(self) -> bool:
        """Kiểm tra kết nối tới Neo4j AuraDB."""
        driver = self.get_driver()
        try:
            await driver.verify_connectivity()
            logger.info("Kết nối tới Neo4j (%s) thành công.", self.uri)
            return True
        except Exception as e:
            logger.error("Không thể kết nối tới Neo4j (%s): %s", self.uri, e)
            raise

    async def close(self) -> None:
        """Đóng driver kết nối."""
        if self._driver is not None:
            await self._driver.close()
            self._driver = None

    async def execute_cypher_file(self, file_path: Optional[Path] = None) -> List[str]:
        """Đọc và thực thi từng câu lệnh Cypher trong file (cắt theo dấu chấm phẩy)."""
        path = file_path or DEFAULT_CONSTRAINTS_FILE
        if not path.exists():
            raise FileNotFoundError(f"Không tìm thấy file cypher tại: {path}")

        content = path.read_text(encoding="utf-8")
        return await self.execute_cypher_script(content)

    async def execute_cypher_script(self, script: str) -> List[str]:
        """Tách các câu lệnh Cypher và thực thi tuần tự."""
        driver = self.get_driver()
        results = []

        # Tách theo dấu chấm phẩy, loại bỏ comment và khoảng trắng
        raw_statements = script.split(";")
        statements = []
        for raw in raw_statements:
            # Loại bỏ single line comments //
            lines = [l for l in raw.splitlines() if not l.strip().startswith("//")]
            cleaned = "\n".join(lines).strip()
            if cleaned:
                statements.append(cleaned)

        async with driver.session(database=self.database) as session:
            for stmt in statements:
                try:
                    await session.run(stmt)
                    results.append(stmt.splitlines()[0])
                    logger.info("Thực thi thành công: %s", stmt.splitlines()[0])
                except Exception as e:
                    logger.error("Lỗi khi thực thi statement:\n%s\nLỗi: %s", stmt, e)
                    raise

        return results

    async def get_active_constraints(self) -> List[dict]:
        """Liệt kê các constraints đang hoạt động trên AuraDB."""
        driver = self.get_driver()
        async with driver.session(database=self.database) as session:
            result = await session.run("SHOW CONSTRAINTS")
            records = [record.data() async for record in result]
            return records
