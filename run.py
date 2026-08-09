#!/usr/bin/env python3
"""Friday entrypoint: uvicorn core.app:app --host 0.0.0.0 --port 8000"""
import os

import uvicorn

if __name__ == "__main__":
    host = os.environ.get("FRIDAY_HOST", "0.0.0.0")
    port = int(os.environ.get("FRIDAY_PORT", "8000"))
    uvicorn.run("core.app:app", host=host, port=port, reload=False)
