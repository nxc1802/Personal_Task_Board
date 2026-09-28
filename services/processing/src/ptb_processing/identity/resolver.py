"""Identity Resolver.

Quy tắc bất biến (docs/v1.md section 6):
1. Tự động resolve/link Person khi khớp chính xác:
   - AAD Object ID
   - Account ID (cùng source/tenant hoặc unique external_id)
   - Email (exact match, case-insensitive)
2. Nếu chỉ trùng họ tên (fuzzy name) hoặc khác tenant alias:
   - Chỉ gắn nhãn candidate signal (confidence thấp hơn, lưu hint)
   - TUYỆT ĐỐI KHÔNG tự động merge vào Person cũ.
"""

from dataclasses import dataclass, field
import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger("ptb.processing.identity.resolver")


class IdentityResolutionResult(BaseModel):
    """Kết quả phân giải danh tính canonical."""

    person_id: Optional[str] = Field(default=None, description="Canonical Person ID nếu resolve thành công")
    canonical_name: Optional[str] = Field(default=None, description="Tên canonical của Person")
    primary_email: Optional[str] = Field(default=None, description="Email chính của Person")
    is_exact_match: bool = Field(default=False, description="True nếu khớp chính xác AAD ID, Account ID hoặc Email")
    is_candidate_signal: bool = Field(
        default=False,
        description="True nếu chỉ khớp fuzzy name hoặc tenant alias khác mà không có exact ID match",
    )
    resolution_method: str = Field(
        default="unresolved",
        description="'aad_object_id' | 'account_id' | 'email' | 'candidate_signal' | 'unresolved' | 'created_new'",
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    candidate_hints: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Danh sách các Person ứng viên tiềm năng nhưng KHÔNG được tự động merge",
    )


@dataclass
class KnownPersonRecord:
    person_id: str
    canonical_name: str
    primary_email: Optional[str] = None
    aad_object_ids: set[str] = field(default_factory=set)
    # account_ids maps (source_type, tenant_id, account_id) or just account_id
    account_ids: set[str] = field(default_factory=set)
    tenant_ids: set[str] = field(default_factory=set)


def normalize_vietnamese_text(text: str) -> str:
    """Loại bỏ dấu tiếng Việt và chuẩn hóa chữ thường để so sánh fuzzy name."""
    if not text:
        return ""
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.replace("đ", "d").replace("Đ", "d")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def names_fuzzy_match(name1: Optional[str], name2: Optional[str]) -> bool:
    """So sánh fuzzy giữa 2 họ tên (ví dụ: 'Nguyen Van Huy' vs 'Huy Nguyen' hoặc 'Nguyễn Văn Huy')."""
    if not name1 or not name2:
        return False
    norm1 = normalize_vietnamese_text(name1)
    norm2 = normalize_vietnamese_text(name2)
    if norm1 == norm2:
        return True
    tokens1 = set(norm1.split())
    tokens2 = set(norm2.split())
    if tokens1 and tokens2 and (tokens1.issubset(tokens2) or tokens2.issubset(tokens1)):
        return True
    return False


class IdentityResolver:
    """Bộ phân giải danh tính tuân thủ quy tắc bất biến của hệ thống."""

    def __init__(self, neo4j_client: Optional[Any] = None) -> None:
        self.neo4j_client = neo4j_client
        self._persons: Dict[str, KnownPersonRecord] = {}

    def register_person(
        self,
        person_id: str,
        canonical_name: str,
        primary_email: Optional[str] = None,
        aad_object_id: Optional[str] = None,
        account_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> None:
        """Đăng ký thông tin Person vào registry nội bộ (dùng cho in-memory hoặc cache)."""
        if person_id not in self._persons:
            self._persons[person_id] = KnownPersonRecord(
                person_id=person_id,
                canonical_name=canonical_name,
                primary_email=primary_email.lower().strip() if primary_email else None,
            )
        person = self._persons[person_id]
        if aad_object_id:
            person.aad_object_ids.add(aad_object_id.strip())
        if account_id:
            person.account_ids.add(account_id.strip())
        if tenant_id:
            person.tenant_ids.add(tenant_id.strip())
        if primary_email:
            person.primary_email = primary_email.lower().strip()

    def resolve(
        self,
        *,
        aad_object_id: Optional[str] = None,
        account_id: Optional[str] = None,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
        tenant_id: Optional[str] = None,
        source_type: Optional[str] = None,
        auto_create_if_not_found: bool = False,
    ) -> IdentityResolutionResult:
        """Phân giải danh tính người dùng theo quy tắc bất biến:

        1. EXACT MATCH:
           - Khớp chính xác aad_object_id -> Tự động link Person (confidence 1.0)
           - Khớp chính xác account_id -> Tự động link Person (confidence 1.0)
           - Khớp chính xác email -> Tự động link Person (confidence 1.0)

        2. FUZZY HOẶC CROSS-TENANT ALIAS:
           - Trùng tên / fuzzy name HOẶC khác tenant alias mà không có ID khớp:
             -> Gắn nhãn candidate signal (confidence 0.50, candidate_hints lưu danh sách ứng viên)
             -> TUYỆT ĐỐI KHÔNG TỰ MERGE vào Person có sẵn.
        """
        norm_email = email.lower().strip() if email else None
        clean_aad_id = aad_object_id.strip() if aad_object_id else None
        clean_account_id = account_id.strip() if account_id else None

        # 1. Kiểm tra EXACT MATCH trên AAD Object ID
        if clean_aad_id:
            for p in self._persons.values():
                if clean_aad_id in p.aad_object_ids:
                    return IdentityResolutionResult(
                        person_id=p.person_id,
                        canonical_name=p.canonical_name,
                        primary_email=p.primary_email,
                        is_exact_match=True,
                        is_candidate_signal=False,
                        resolution_method="aad_object_id",
                        confidence=1.0,
                    )

        # 2. Kiểm tra EXACT MATCH trên Account ID
        if clean_account_id:
            for p in self._persons.values():
                if clean_account_id in p.account_ids:
                    return IdentityResolutionResult(
                        person_id=p.person_id,
                        canonical_name=p.canonical_name,
                        primary_email=p.primary_email,
                        is_exact_match=True,
                        is_candidate_signal=False,
                        resolution_method="account_id",
                        confidence=1.0,
                    )

        # 3. Kiểm tra EXACT MATCH trên Email
        if norm_email:
            for p in self._persons.values():
                if p.primary_email and p.primary_email == norm_email:
                    return IdentityResolutionResult(
                        person_id=p.person_id,
                        canonical_name=p.canonical_name,
                        primary_email=p.primary_email,
                        is_exact_match=True,
                        is_candidate_signal=False,
                        resolution_method="email",
                        confidence=1.0,
                    )

        # 4. KHÔNG CÓ EXACT MATCH -> Kiểm tra xem có candidate signal (fuzzy name hoặc cross-tenant alias)
        candidate_hints: List[Dict[str, Any]] = []

        if display_name:
            for p in self._persons.values():
                # Fuzzy name matching
                if names_fuzzy_match(display_name, p.canonical_name):
                    reason = "Fuzzy name match"
                    if tenant_id and p.tenant_ids and tenant_id not in p.tenant_ids:
                        reason += " across different tenant alias"
                    candidate_hints.append({
                        "person_id": p.person_id,
                        "canonical_name": p.canonical_name,
                        "primary_email": p.primary_email,
                        "reason": f"{reason}. Auto-merge disallowed by invariant rule.",
                    })

        if candidate_hints:
            # Gắn nhãn candidate signal: KHÔNG tự merge vào bất kỳ Person nào ở trên
            new_person_id = str(uuid4()) if auto_create_if_not_found else None
            return IdentityResolutionResult(
                person_id=new_person_id,
                canonical_name=display_name,
                primary_email=norm_email,
                is_exact_match=False,
                is_candidate_signal=True,
                resolution_method="candidate_signal",
                confidence=0.50,  # Thấp hơn ngưỡng auto-accept 0.65
                candidate_hints=candidate_hints,
            )

        # 5. Hoàn toàn không tìm thấy
        if auto_create_if_not_found:
            new_id = str(uuid4())
            self.register_person(
                person_id=new_id,
                canonical_name=display_name or "Unknown",
                primary_email=norm_email,
                aad_object_id=clean_aad_id,
                account_id=clean_account_id,
                tenant_id=tenant_id,
            )
            return IdentityResolutionResult(
                person_id=new_id,
                canonical_name=display_name or "Unknown",
                primary_email=norm_email,
                is_exact_match=True,
                is_candidate_signal=False,
                resolution_method="created_new",
                confidence=1.0,
            )

        return IdentityResolutionResult(
            person_id=None,
            canonical_name=display_name,
            primary_email=norm_email,
            is_exact_match=False,
            is_candidate_signal=False,
            resolution_method="unresolved",
            confidence=0.0,
            candidate_hints=[],
        )

    async def resolve_async(
        self,
        *,
        aad_object_id: Optional[str] = None,
        account_id: Optional[str] = None,
        email: Optional[str] = None,
        display_name: Optional[str] = None,
        tenant_id: Optional[str] = None,
        source_type: Optional[str] = None,
        auto_create_if_not_found: bool = False,
    ) -> IdentityResolutionResult:
        """Async resolve hỗ trợ tra cứu trực tiếp từ Neo4j nếu neo4j_client khả dụng,

        sau đó fallback về in-memory registry.
        """
        if self.neo4j_client is None:
            return self.resolve(
                aad_object_id=aad_object_id,
                account_id=account_id,
                email=email,
                display_name=display_name,
                tenant_id=tenant_id,
                source_type=source_type,
                auto_create_if_not_found=auto_create_if_not_found,
            )

        driver = self.neo4j_client.get_driver()
        norm_email = email.lower().strip() if email else None
        clean_aad_id = aad_object_id.strip() if aad_object_id else None
        clean_account_id = account_id.strip() if account_id else None

        async with driver.session(database=self.neo4j_client.database) as session:
            # 1. Exact match trên AAD Object ID hoặc Account ID qua SourceIdentity
            if clean_aad_id or clean_account_id:
                target_id = clean_aad_id or clean_account_id
                query = """
                MATCH (p:Person)-[:HAS_IDENTITY]->(s:SourceIdentity)
                WHERE s.external_id = $ext_id OR s.aad_object_id = $ext_id
                RETURN p.canonical_id AS person_id, p.canonical_name AS name, p.primary_email AS email
                LIMIT 1
                """
                res = await session.run(query, {"ext_id": target_id})
                record = await res.single()
                if record:
                    method = "aad_object_id" if clean_aad_id else "account_id"
                    return IdentityResolutionResult(
                        person_id=record["person_id"],
                        canonical_name=record["name"],
                        primary_email=record["email"],
                        is_exact_match=True,
                        is_candidate_signal=False,
                        resolution_method=method,
                        confidence=1.0,
                    )

            # 2. Exact match trên primary_email
            if norm_email:
                query_email = """
                MATCH (p:Person)
                WHERE toLower(p.primary_email) = $email
                RETURN p.canonical_id AS person_id, p.canonical_name AS name, p.primary_email AS email
                LIMIT 1
                """
                res = await session.run(query_email, {"email": norm_email})
                record = await res.single()
                if record:
                    return IdentityResolutionResult(
                        person_id=record["person_id"],
                        canonical_name=record["name"],
                        primary_email=record["email"],
                        is_exact_match=True,
                        is_candidate_signal=False,
                        resolution_method="email",
                        confidence=1.0,
                    )

            # 3. Fuzzy name search trên Neo4j -> Candidate signal ONLY (Never auto-merge)
            if display_name:
                fuzzy_query = """
                MATCH (p:Person)
                WHERE toLower(p.canonical_name) CONTAINS toLower($name)
                   OR toLower($name) CONTAINS toLower(p.canonical_name)
                RETURN p.canonical_id AS person_id, p.canonical_name AS name, p.primary_email AS email
                LIMIT 5
                """
                res = await session.run(fuzzy_query, {"name": display_name.strip()})
                records = await res.data()
                if records:
                    candidate_hints = [
                        {
                            "person_id": r["person_id"],
                            "canonical_name": r["name"],
                            "primary_email": r["email"],
                            "reason": "Fuzzy name match in Neo4j. Auto-merge disallowed by invariant rule.",
                        }
                        for r in records
                    ]
                    return IdentityResolutionResult(
                        person_id=None,
                        canonical_name=display_name,
                        primary_email=norm_email,
                        is_exact_match=False,
                        is_candidate_signal=True,
                        resolution_method="candidate_signal",
                        confidence=0.50,
                        candidate_hints=candidate_hints,
                    )

        # Fallback về in-memory logic
        return self.resolve(
            aad_object_id=aad_object_id,
            account_id=account_id,
            email=email,
            display_name=display_name,
            tenant_id=tenant_id,
            source_type=source_type,
            auto_create_if_not_found=auto_create_if_not_found,
        )
