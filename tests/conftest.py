import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = ROOT / ".claude" / "skills" / "book-to-question" / "scripts"

for path in (str(ROOT), str(SKILL_SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)
