"""
Utility scripts package.

Imports from this package must remain **side-effect free** so cron jobs and one-shot
tools can run ``from scripts.weekly_report import generate_report`` without
starting the async strategy loop or touching live credentials.
"""
