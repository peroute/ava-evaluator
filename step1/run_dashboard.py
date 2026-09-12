"""Start the local Step 1 inspection dashboard.

    python run_dashboard.py

Then open http://127.0.0.1:8000/ in a browser. Local development only:
read-only, no authentication.
"""

import uvicorn

HOST = "127.0.0.1"
PORT = 8000

if __name__ == "__main__":
    print("Step 1 Inspector running at http://%s:%d/ (Ctrl+C to stop)" % (HOST, PORT))
    uvicorn.run("web.api:app", host=HOST, port=PORT, reload=False)
