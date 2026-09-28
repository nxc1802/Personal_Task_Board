from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ============================================================
# PATH DEFINITIONS
# ============================================================

DATA = PROJECT_ROOT / "data" / "teams"

CONFIG_DIR = PROJECT_ROOT / "config" / "teams" 

CHAT_INDEX_FILE = DATA / "chat_index.json"