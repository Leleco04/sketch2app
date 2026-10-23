"""Preparação local de desenhos, sem rede e sem registrar a imagem."""

import base64
import binascii
import os
import warnings
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_B64_CHARS = 4 * ((MAX_IMAGE_BYTES + 2) // 3)


class ImageProcessingError(ValueError):
    """Imagem ou configuração inválida para o pré-processamento."""


class ImageTooLargeError(ImageProcessingError):
    """O arquivo enviado excede o limite de entrada da API."""


def decode_image_b64(value: str) -> bytes:
    """Decodifica base64 puro, sem data URL, com limite de 10 MiB."""
    if not isinstance(value, str) or not value:
        raise ImageProcessingError("Informe uma imagem em base64 sem prefixo.")
    if len(value) > MAX_IMAGE_B64_CHARS:
        raise ImageTooLargeError("A imagem deve ter no máximo 10 MiB.")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ImageProcessingError("Base64 inválido. Envie a imagem sem prefixo data:.") from None
    if not data:
        raise ImageProcessingError("A imagem está vazia.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageTooLargeError("A imagem deve ter no máximo 10 MiB.")
    return data


def preprocess_image(
    source: bytes | str | Path, *, max_side: int | None = None
) -> bytes:
    """Retorna JPEG em cinza; bytes são a entrada prevista para a futura API.

    Caminhos locais atendem ao smoke test manual. Imagens pequenas não são
    ampliadas, e o conteúdo e os caminhos não aparecem nas mensagens de erro.
    """
    if max_side is None:
        try:
            max_side = int(os.getenv("MAX_IMAGE_SIDE", "1024"))
        except ValueError:
            raise ImageProcessingError("MAX_IMAGE_SIDE deve ser um inteiro positivo.") from None
    if isinstance(max_side, bool) or not isinstance(max_side, int) or max_side <= 0:
        raise ImageProcessingError("MAX_IMAGE_SIDE deve ser um inteiro positivo.")
    if not isinstance(source, (bytes, str, Path)):
        raise ImageProcessingError("Informe bytes de uma imagem ou um arquivo local.")

    image_source = BytesIO(source) if isinstance(source, bytes) else source
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(image_source) as original:
                image = ImageOps.exif_transpose(original)
                if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                    rgba = image.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, "white")
                    image = Image.alpha_composite(background, rgba)
                image = ImageOps.autocontrast(image.convert("L"))
                image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                output = BytesIO()
                image.save(output, format="JPEG", quality=85)
                return output.getvalue()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageProcessingError("A imagem excede o limite seguro de pixels.") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise ImageProcessingError(
            "Não foi possível ler a imagem. Envie um arquivo de imagem válido."
        ) from None
