"""Celery: the background scheduler.

Radar Monitors are the only thing here today. Beat wakes the dispatcher every
few minutes; the dispatcher sends whichever monitors' random slots have come
round to n8n, a steady few at a time. See app/tasks/monitors.py.
"""
