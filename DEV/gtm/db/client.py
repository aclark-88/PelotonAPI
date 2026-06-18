"""Singleton Supabase client.

Reads SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the environment or a
.env file in the working directory. The service role key bypasses RLS — this
client is for trusted orchestration code only, never a frontend.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from supabase import Client, create_client


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    supabase_url: str
    supabase_service_role_key: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_client() -> Client:
    s = get_settings()
    return create_client(s.supabase_url, s.supabase_service_role_key)
