"""Run the PC-side NAS Library Auditor.

Use this on the desktop PC, not inside the Synology container:

    python run_pc_auditor.py

Then open http://127.0.0.1:8787
"""

import uvicorn


if __name__ == "__main__":
    print("NAS Library Auditor: http://127.0.0.1:8787")
    uvicorn.run("pc_auditor.app:app", host="127.0.0.1", port=8787, log_level="info")
