#!/usr/bin/env python3

import subprocess
import time
import sys
import os

# Start the backend with DEBUG logging
backend_env = os.environ.copy()
backend_env.update({
    "TESTING": "1",
    "DEV_ADMIN": "1",
    "WORKER_ID": "0",
    "LOG_LEVEL": "DEBUG"
})

backend_process = subprocess.Popen(
    ["backend/.venv/bin/python", "-m", "uvicorn", "zerg.main:app", "--port", "8001", "--log-level", "debug"],
    cwd="backend",
    env=backend_env,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1
)

# Start log capture in background
def capture_logs():
    for line in backend_process.stdout:
        if "DEBUG:" in line or "execution_finished" in line or "subscribed to workflow" in line:
            print(f"BACKEND: {line.strip()}", flush=True)

import threading
log_thread = threading.Thread(target=capture_logs)
log_thread.daemon = True
log_thread.start()

# Wait for backend to start
time.sleep(5)

# Run the test
try:
    test_result = subprocess.run(
        ["./e2e/run_e2e_tests.sh", "tests/simple_run_test.spec.ts"],
        capture_output=True,
        text=True
    )
    print("TEST OUTPUT:")
    print(test_result.stdout)
    print(test_result.stderr)
finally:
    # Kill backend
    backend_process.terminate()
    backend_process.wait()
