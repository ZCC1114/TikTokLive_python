#!/bin/bash
cd /home/ec2-user/fastsort/tiktokLive/TikTokLive-python
source .venv/bin/activate
export PYTHONPATH=$(pwd)
python examples/fastapi_ws_server.py
