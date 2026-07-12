from pydantic import BaseModel, Field
from typing import Optional, List, Literal


class PanelInfo(BaseModel):
    id: str
    name: str
    name_fa: str
    icon: str
    support_level: Literal["full", "partial", "experimental", "db_only"]
    subscription_preserved: bool
    description_fa: str
    warnings_fa: List[str] = []
    supported_source_dbs: List[str]
    requires_pasarguard: bool
    requires_source_installed: bool


class PrerequisiteCheck(BaseModel):
    ok: bool
    checks: List[dict]
    message_fa: str


class DatabaseOption(BaseModel):
    id: str
    name: str
    name_fa: str
    recommended: bool = False
    reason_fa: str = ""


class MigrationRequest(BaseModel):
    source_panel: str
    source_db: str
    source_db_password: Optional[str] = None
    source_db_host: str = "127.0.0.1"
    source_db_port: Optional[int] = None
    target_db: str
    target_db_password: Optional[str] = None
    upload_id: Optional[str] = None
    install_redirect: bool = False
    pasarguard_install: bool = False


class MigrationStatus(BaseModel):
    job_id: str
    status: Literal["pending", "running", "success", "error"]
    progress: int = 0
    message_fa: str = ""
    logs: List[str] = []
    result: Optional[dict] = None
