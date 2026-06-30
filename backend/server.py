#!/usr/bin/env python3
import os

import uvicorn


def main():
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        reload=os.environ.get("RELOAD", "false").lower() == "true",
    )


if __name__ == "__main__":
    main()
