"""Fila de aprovação de desenhos enviados pela plateia via túnel."""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from app import imaging
from app.models import QueueItem
from app.qrcode_svg import render_qr_svg

ALLOWED_UPLOAD_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
MAX_QUEUE_ITEMS = 30
UPLOAD_MIN_INTERVAL_S = 8.0
_QID_RE = re.compile(r"^[0-9a-f]{32}$")


class AudienceUploadError(ValueError):
    def __init__(self, code: str, msg: str, status_code: int = 400) -> None:
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.status_code = status_code


class AudienceQueue:
    def __init__(
        self,
        queue_dir: str | Path | None = None,
        *,
        min_interval_s: float = UPLOAD_MIN_INTERVAL_S,
        max_items: int = MAX_QUEUE_ITEMS,
    ) -> None:
        base = Path(__file__).resolve().parent.parent / "generated" / "queue"
        self.queue_dir = Path(queue_dir if queue_dir is not None else base).resolve()
        self.min_interval_s = max(0.0, float(min_interval_s))
        self.max_items = max(1, int(max_items))
        self._last_upload_by_ip: dict[str, float] = {}

    def reset_rate_limits(self) -> None:
        self._last_upload_by_ip.clear()

    def _check_rate_limit(self, client_key: str, now: float | None = None) -> None:
        if self.min_interval_s <= 0:
            return
        ts = time.monotonic() if now is None else now
        key = (client_key or "unknown").strip() or "unknown"
        last = self._last_upload_by_ip.get(key)
        if last is not None and (ts - last) < self.min_interval_s:
            remaining = max(1, int(round(self.min_interval_s - (ts - last))))
            raise AudienceUploadError(
                "rate_limit",
                f"Aguarde {remaining} s antes de enviar outro desenho.",
                status_code=429,
            )
        if len(self._last_upload_by_ip) > 512:
            cutoff = ts - self.min_interval_s * 2
            self._last_upload_by_ip = {
                k: v for k, v in self._last_upload_by_ip.items() if v >= cutoff
            }
        self._last_upload_by_ip[key] = ts

    @staticmethod
    def validate_and_preprocess(image_b64: str) -> bytes:
        try:
            raw = imaging.decode_image_b64(image_b64)
        except imaging.ImageProcessingError as error:
            status = 413 if "10 MiB" in str(error) else 422
            raise AudienceUploadError("invalid_image", str(error), status_code=status) from None

        try:
            with Image.open(BytesIO(raw)) as img:
                fmt = (img.format or "").upper()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise AudienceUploadError(
                "invalid_image",
                "Formato de imagem inválido. Use JPEG, PNG ou WebP.",
                status_code=422,
            ) from None

        if fmt not in ALLOWED_UPLOAD_FORMATS:
            raise AudienceUploadError(
                "invalid_image",
                "Formato não permitido. Envie apenas JPEG, PNG ou WebP.",
                status_code=422,
            )

        try:
            return imaging.preprocess_image(raw)
        except imaging.ImageProcessingError as error:
            raise AudienceUploadError("invalid_image", str(error), status_code=422) from None

    def enqueue(
        self,
        *,
        image_b64: str,
        author: str = "",
        note: str = "",
        client_key: str = "unknown",
    ) -> QueueItem:
        self._check_rate_limit(client_key)
        existing = self.list_items()
        if len(existing) >= self.max_items:
            raise AudienceUploadError(
                "queue_full",
                "A fila de desenhos da plateia está cheia no momento.",
                status_code=429,
            )

        prepared_jpeg = self.validate_and_preprocess(image_b64)
        qid = uuid4().hex
        item = QueueItem(
            qid=qid,
            author=(author or "").strip()[:60],
            note=(note or "").strip()[:240],
            created_at=datetime.now(timezone.utc),
        )
        try:
            self.queue_dir.mkdir(parents=True, exist_ok=True)
            img_path = self.queue_dir / f"{qid}.jpg"
            meta_path = self.queue_dir / f"{qid}.json"
            tmp_meta = self.queue_dir / f".tmp-{qid}.json"
            img_path.write_bytes(prepared_jpeg)
            tmp_meta.write_text(
                item.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp_meta, meta_path)
        except OSError as exc:
            raise AudienceUploadError(
                "storage_error",
                "Não foi possível salvar o desenho na fila.",
                status_code=500,
            ) from exc
        return item

    def list_items(self) -> list[QueueItem]:
        if not self.queue_dir.is_dir():
            return []
        items: list[QueueItem] = []
        try:
            entries = sorted(self.queue_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            return []
        for entry in entries:
            if not entry.is_file() or entry.suffix != ".json" or entry.name.startswith("."):
                continue
            qid = entry.stem
            if not _QID_RE.fullmatch(qid):
                continue
            img_path = self.queue_dir / f"{qid}.jpg"
            if not img_path.is_file():
                continue
            try:
                item = QueueItem.model_validate_json(entry.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValidationError):
                continue
            if item.qid == qid:
                items.append(item)
        items.sort(key=lambda item: item.created_at)
        return items

    def _resolve_paths(self, qid: str) -> tuple[Path, Path]:
        if not isinstance(qid, str) or not _QID_RE.fullmatch(qid):
            raise AudienceUploadError("not_found", "Desenho não encontrado na fila.", status_code=404)
        meta_path = (self.queue_dir / f"{qid}.json").resolve()
        img_path = (self.queue_dir / f"{qid}.jpg").resolve()
        if meta_path.parent != self.queue_dir or img_path.parent != self.queue_dir:
            raise AudienceUploadError("not_found", "Desenho não encontrado na fila.", status_code=404)
        if not meta_path.is_file() or not img_path.is_file():
            raise AudienceUploadError("not_found", "Desenho não encontrado na fila.", status_code=404)
        return meta_path, img_path

    def get_item(self, qid: str) -> QueueItem:
        meta_path, _ = self._resolve_paths(qid)
        try:
            return QueueItem.model_validate_json(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValidationError) as exc:
            raise AudienceUploadError(
                "not_found", "Desenho não encontrado na fila.", status_code=404
            ) from exc

    def read_image(self, qid: str) -> bytes:
        _, img_path = self._resolve_paths(qid)
        try:
            return img_path.read_bytes()
        except OSError as exc:
            raise AudienceUploadError(
                "storage_error", "Não foi possível ler a imagem da fila.", status_code=500
            ) from exc

    def remove_item(self, qid: str) -> None:
        meta_path, img_path = self._resolve_paths(qid)
        for path in (meta_path, img_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def get_public_upload_url() -> str | None:
    base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not base:
        return None
    if not (base.startswith("http://") or base.startswith("https://")):
        return None
    return f"{base}/enviar"


def get_public_qr_svg(url: str | None = None) -> str | None:
    target = url if url is not None else get_public_upload_url()
    if not target:
        return None
    try:
        return render_qr_svg(target)
    except ValueError:
        return None
