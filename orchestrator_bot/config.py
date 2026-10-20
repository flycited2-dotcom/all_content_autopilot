"""Настройки сервиса оркестратора из окружения (/opt/agents-my/.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    bot_token: str
    owner_chat_id: int
    api_token: str
    db_path: str = "/opt/agents-my/state/orchestrator.db"
    api_port: int = 8790
    cf_root: str = "/opt/content-factory"
    cf_python: str = "/opt/content-factory/.venv/bin/python"
    doubt_threshold: int = 40
    whisper_model: str = "small"

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        env = os.environ if env is None else env
        return cls(
            bot_token=env["ORCH_BOT_TOKEN"],
            owner_chat_id=int(env["ORCH_OWNER_CHAT_ID"]),
            api_token=env["ORCH_API_TOKEN"],
            db_path=env.get("ORCH_DB", cls.db_path),
            api_port=int(env.get("ORCH_API_PORT", cls.api_port)),
            cf_root=env.get("ORCH_CF_ROOT", cls.cf_root),
            cf_python=env.get("ORCH_CF_PYTHON", cls.cf_python),
            doubt_threshold=int(env.get("ORCH_DOUBT_THRESHOLD", cls.doubt_threshold)),
            whisper_model=env.get("ORCH_WHISPER_MODEL", cls.whisper_model),
        )
