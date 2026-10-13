import os
from pathlib import Path

state = Path(os.environ["STATE_FILE"])
count = int(state.read_text()) + 1 if state.exists() else 1
state.write_text(str(count))
print("usage limit reached" if count < 3 else "ok")
