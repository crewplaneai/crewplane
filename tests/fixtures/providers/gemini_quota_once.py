import os
from pathlib import Path

state = Path(os.environ["STATE_FILE"])
count = int(state.read_text()) + 1 if state.exists() else 1
state.write_text(str(count))
if count < 2:
    print(
        "You have exhausted your capacity on this model. Your quota will reset after 2s."
    )
else:
    print('{"response":"ok"}')
