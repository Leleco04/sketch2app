"""Contratos tipados da API e dos metadados persistidos."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.imaging import MAX_IMAGE_B64_CHARS

SID_PATTERN = r"^[0-9a-f]{32}$"
CACHE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class CreateSessionRequest(Model):
    image_b64: str | None = Field(default=None, max_length=MAX_IMAGE_B64_CHARS, repr=False)
    prompt: str | None = None
    use_extras: bool = False
    use_data_api: bool = False

    @model_validator(mode="after")
    def validate_input(self) -> "CreateSessionRequest":
        self.prompt = (self.prompt or "").strip()
        self.image_b64 = (self.image_b64 or "").strip() or None
        if not self.prompt and self.image_b64 is None:
            raise ValueError("Informe uma imagem e/ou um pedido em texto.")
        try:
            self.prompt.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("O pedido contém texto inválido para UTF-8.") from None
        return self


class ChatMessageRequest(Model):
    text: str = Field(max_length=4000, repr=False)
    reattach_image: bool = False
    use_extras: bool = False
    use_data_api: bool = False

    @model_validator(mode="after")
    def validate_text(self) -> "ChatMessageRequest":
        self.text = self.text.strip()
        if not self.text:
            raise ValueError("Informe uma mensagem de ajuste.")
        try:
            self.text.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("A mensagem contém texto inválido para UTF-8.") from None
        return self


class RestoreVersionRequest(Model):
    version: int = Field(ge=1, strict=True)


class GenerationResult(Model):
    sid: str = Field(pattern=SID_PATTERN)
    version: int = Field(ge=1)
    url: str
    summary: str


class FrontendConfig(Model):
    mode: Literal["mock", "live", "offline"]
    max_image_bytes: int = Field(gt=0)


class ErrorResponse(Model):
    code: str
    msg: str
    hint: str | None = None


class StageTiming(Model):
    step: Literal["preparando", "enviando", "gerando", "validando", "gravando"]
    ms: int = Field(ge=0)
    wait_ms: int = Field(default=0, ge=0)
    attempt: int | None = Field(default=None, ge=1)


class AttemptUsage(Model):
    attempt: int = Field(default=1, ge=1)
    model: str = Field(min_length=1)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    tokens_thinking: int | None = Field(default=None, ge=0)
    latency_ms: int = Field(ge=0)
    wait_ms: int = Field(default=0, ge=0)
    calls_today: int = Field(default=0, ge=0)
    simulated: bool = False


class VersionInfo(Model):
    n: int = Field(ge=1)
    summary: str
    ts: AwareDatetime
    analysis: list[str] | None = None
    usage: list[AttemptUsage] | None = None
    stage_timing: list[StageTiming] | None = None


class Message(Model):
    role: Literal["user", "assistant"]
    text: str
    ts: AwareDatetime


class SessionRecord(Model):
    sid: str = Field(pattern=SID_PATTERN)
    prompt: str
    created_at: AwareDatetime
    original_image: Literal["original.bin"] | None = None
    prepared_image: Literal["image.jpg"] | None = None
    replay_source: str | None = Field(default=None, pattern=CACHE_NAME_PATTERN)
    current: int = Field(ge=1)
    versions: list[VersionInfo] = Field(min_length=1)
    messages: list[Message]

    @model_validator(mode="after")
    def validate_versions(self) -> "SessionRecord":
        numbers = [version.n for version in self.versions]
        if len(numbers) != len(set(numbers)) or self.current not in numbers:
            raise ValueError("Histórico de versões inconsistente.")
        if (self.original_image is None) != (self.prepared_image is None):
            raise ValueError("Referências de imagem inconsistentes.")
        return self


class SessionDetailResponse(Model):
    sid: str = Field(pattern=SID_PATTERN)
    current: int = Field(ge=1)
    has_image: bool = False
    versions: list[VersionInfo]
    messages: list[Message]


class CachedVersion(Model):
    n: int = Field(ge=1)
    summary: str
    html: str
    ts: AwareDatetime | None = None
    analysis: list[str] | None = None
    usage: list[AttemptUsage] | None = None
    stage_timing: list[StageTiming] | None = None


class CachedMessage(Model):
    role: Literal["user", "assistant"]
    text: str
    ts: AwareDatetime | None = None


class CachedSession(Model):
    name: str | None = Field(default=None, pattern=CACHE_NAME_PATTERN)
    prompt: str = ""
    created_at: AwareDatetime | None = None
    has_image: bool = False
    original_image_b64: str | None = Field(default=None, repr=False)
    prepared_image_b64: str | None = Field(default=None, repr=False)
    current: int = Field(default=1, ge=1)
    versions: list[CachedVersion] = Field(min_length=1)
    messages: list[CachedMessage] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_cached_versions(self) -> "CachedSession":
        numbers = [version.n for version in self.versions]
        if len(numbers) != len(set(numbers)) or 1 not in numbers:
            raise ValueError("Gravação de cache com versões inconsistentes.")
        return self


class CacheEntryInfo(Model):
    name: str = Field(pattern=CACHE_NAME_PATTERN)
    prompt: str
    summary: str
    versions: int = Field(ge=1)
    has_image: bool = False
    has_metrics: bool = False


class CacheListResponse(Model):
    items: list[CacheEntryInfo]


class PromptsResponse(Model):
    generate: str
    generate_text: str
    chat: str
    extras: str = ""


class ProjectSummary(Model):
    sid: str = Field(pattern=SID_PATTERN)
    title: str
    prompt: str = ""
    summary: str = ""
    current: int = Field(ge=1)
    versions_count: int = Field(ge=1)
    versions: list[int] = Field(default_factory=list)
    has_image: bool = False
    created_at: AwareDatetime
    synced_cloud: bool = False


class HubListResponse(Model):
    backend: Literal["disk", "supabase"]
    items: list[ProjectSummary]


class UploadDrawingRequest(Model):
    image_b64: str = Field(max_length=MAX_IMAGE_B64_CHARS, repr=False)
    author: str | None = Field(default=None, max_length=60)
    note: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def validate_upload(self) -> "UploadDrawingRequest":
        self.image_b64 = (self.image_b64 or "").strip()
        self.author = (self.author or "").strip()
        self.note = (self.note or "").strip()
        if not self.image_b64:
            raise ValueError("Envie uma imagem do desenho.")
        try:
            self.author.encode("utf-8")
            self.note.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Os campos contêm texto inválido para UTF-8.") from None
        return self


class QueueItem(Model):
    qid: str = Field(pattern=SID_PATTERN)
    author: str = ""
    note: str = ""
    created_at: AwareDatetime


class AudienceQueueResponse(Model):
    public_url: str | None = None
    qr_svg: str | None = None
    items: list[QueueItem]

