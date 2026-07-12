from pydantic import BaseModel
from typing import Optional, List, Literal


class PanelPrerequisites(BaseModel):
    pasarguard_required: bool
    pasarguard_required_before: bool
    source_panel_required: bool
    source_panel_required_before: bool
    install_notes: dict[str, str]  # en, fa, ru


class PanelInfo(BaseModel):
    id: str
    name: dict[str, str]
    icon: str
    support_level: Literal["full", "partial", "experimental", "db_only"]
    subscription_mode: Literal["native", "redirect", "changed"]
    description: dict[str, str]
    warnings: dict[str, List[str]]
    prerequisites: PanelPrerequisites
    supported_source_dbs: List[str]


class MigrationRequest(BaseModel):
    source_panel: str
    source_db: str
    source_db_password: Optional[str] = None
    source_db_host: str = "127.0.0.1"
    source_db_port: Optional[int] = None
    target_db: str
    target_db_password: Optional[str] = None
    upload_id: Optional[str] = None
    install_redirect: bool = True
    pasarguard_install: bool = False
    remnawave_url: Optional[str] = None
    remnawave_token: Optional[str] = None


class MigrationStatus(BaseModel):
    job_id: str
    status: Literal["pending", "running", "success", "error"]
    progress: int = 0
    message: str = ""
    logs: List[str] = []
    result: Optional[dict] = None
