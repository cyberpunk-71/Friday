#!/usr/bin/env python3
"""Friday Focus entrypoint: uvicorn focusapp.server:app."""
import os
import uvicorn

if __name__ == "__main__":
    host = os.environ.get("FRIDAY_HOST", "0.0.0.0")
    port = int(os.environ.get("FRIDAY_PORT", "8010"))
    uvicorn.run("focusapp.server:app", host=host, port=port, reload=False)
