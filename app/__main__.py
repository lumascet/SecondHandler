"""`python -m app` starts the dashboard (HOST/PORT env, default 0.0.0.0:8000)."""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")),
                # open streams (the agents' SSE) are cut after a few seconds, so the lifespan's cleanup (browser,
                # recordings, counts) runs well inside the stop timeout (supervisord 20 s, compose 40 s)
                proxy_headers=True, timeout_graceful_shutdown=5, log_level="info")
