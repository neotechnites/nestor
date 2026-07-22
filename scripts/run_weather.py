#!/usr/bin/env python3
"""Entrypoint the cron/systemd timer calls once a day (~9am ET).
Loads .env, then runs the weather sleeve."""
import os
import sys

# make the repo root importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from nestor.weather_bot import run

if __name__ == "__main__":
    run()
