from pathlib import Path

RAW_DIR = Path("data/outlook/raw")
PARSED_DIR = Path("data/outlook/parsed")

RAW_DIR.mkdir(parents=True, exist_ok=True)
PARSED_DIR.mkdir(parents=True, exist_ok=True)

CLICK_RETRIES = 3
