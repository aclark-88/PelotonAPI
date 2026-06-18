"""Orchestration skills for the Clarion GTM system.

Each skill module exposes run(ctx: SkillContext, **kwargs) -> SkillResult.
Skills never call each other; data passes through Supabase.
"""
