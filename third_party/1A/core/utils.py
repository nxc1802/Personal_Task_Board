import re
from datetime import datetime

# ============================================================
# HELPER FUNCTIONS
# ============================================================

def sanitize_filename(name: str) -> str:
    """Chuẩn hóa tên file phù hợp với Windows."""

    if not name:
        return "Untitled"

    name = re.sub(r'[<>:"/\\|?*+=]', "_", name)
    name = re.sub(r"\s+", "_", name.strip())
    name = re.sub(r"_+", "_", name)

    return name.strip("._") or "Untitled"


def clean_text(value: str | None) -> str:
    """
    Chuẩn hóa chuỗi văn bản.
    """

    if not value:
        return ""

    return re.sub(r"\s+", " ", value).strip()

# ============================================================
# SORT MESSAGES
# ============================================================

def sort_messages(messages: list[dict]) -> list:

    def get_sort_key(message):
        timestamp = message.get("timestamp")

        try:
            return (
                0,
                datetime.fromisoformat(
                    timestamp.replace(
                        "Z",
                        "+00:00",
                    )
                ),
            )
        except Exception:
            return (
                1,
                message.get(
                    "collection_index",
                    0,
                ),
            )

    return sorted(
        messages,
        key=get_sort_key,
    )