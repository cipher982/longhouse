#!/bin/bash
# Thin wrapper - longhouse serve handles everything
exec longhouse serve --host 0.0.0.0 --port 8000
