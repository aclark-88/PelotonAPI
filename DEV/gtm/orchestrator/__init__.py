"""Orchestrator entry points — the schedulable scripts.

Each module is runnable: py -m gtm.orchestrator.<name> [--dry-run]
Skills never call each other; these scripts sequence them and pass data
through Supabase only.
"""
