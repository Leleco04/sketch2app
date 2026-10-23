"""Imagens sintéticas verificam o pré-processamento sem rede."""

import base64
from io import BytesIO

import pytest
from PIL import Image

from app import imaging
from app.imaging import ImageProcessingError, ImageTooLargeError, decode_image_b64, preprocess_image


def image_bytes(image: Image.Image, *, format: str = "PNG", **options) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format=format, **options)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "size,expected",
    [((1600, 800), (1024, 512)), ((800, 1600), (512, 1024)), ((64, 32), (64, 32))],
)
def test_jpeg_grayscale_dimensions_and_aspect_ratio(size, expected, monkeypatch):
    monkeypatch.delenv("MAX_IMAGE_SIDE", raising=False)
    result = preprocess_image(image_bytes(Image.new("RGB", size, "red")))
    with Image.open(BytesIO(result)) as image:
        assert image.format == "JPEG"
        assert image.mode == "L"
        assert image.size == expected


def test_autocontrast_preserves_intermediate_pencil_tones():
    image = Image.new("L", (240, 80))
    for x in range(image.width):
        for y in range(image.height):
            image.putpixel((x, y), 100 + x // 4)
    result = preprocess_image(image_bytes(image), max_side=1024)
    with Image.open(BytesIO(result)) as prepared:
        low, high = prepared.getextrema()
        assert low < 10
        assert high > 245
        assert len(set(prepared.tobytes())) > 20


def test_environment_limit_and_explicit_override(monkeypatch):
    monkeypatch.setenv("MAX_IMAGE_SIDE", "80")
    data = image_bytes(Image.new("RGB", (400, 200)))
    with Image.open(BytesIO(preprocess_image(data))) as image:
        assert image.size == (80, 40)
    with Image.open(BytesIO(preprocess_image(data, max_side=120))) as image:
        assert image.size == (120, 60)


@pytest.mark.parametrize("max_side", [0, -1, True, 1.5, "1024"])
def test_invalid_explicit_size(max_side):
    with pytest.raises(ImageProcessingError, match="inteiro positivo"):
        preprocess_image(b"", max_side=max_side)


@pytest.mark.parametrize("value", ["", "invalid", "0", "-1", "2.5"])
def test_invalid_environment_size(value, monkeypatch):
    monkeypatch.setenv("MAX_IMAGE_SIDE", value)
    with pytest.raises(ImageProcessingError, match="inteiro positivo"):
        preprocess_image(b"")


def test_local_path_and_string_path(tmp_path):
    path = tmp_path / "desenho.png"
    path.write_bytes(image_bytes(Image.new("RGB", (100, 50), "white")))
    assert preprocess_image(path, max_side=100) == preprocess_image(str(path), max_side=100)


@pytest.mark.parametrize("kind", ["missing", "directory", "corrupt"])
def test_invalid_paths_have_controlled_errors_without_path_disclosure(tmp_path, kind):
    path = tmp_path / "private-image.png"
    if kind == "directory":
        path.mkdir()
    elif kind == "corrupt":
        path.write_bytes(b"not an image")
    with pytest.raises(ImageProcessingError, match="imagem válido") as error:
        preprocess_image(path, max_side=1024)
    assert str(path) not in str(error.value)


@pytest.mark.parametrize("data", [b"", b"not an image", b"\x89PNG\r\n\x1a\n", None, 123])
def test_invalid_image_input(data):
    with pytest.raises(ImageProcessingError):
        preprocess_image(data, max_side=1024)


def test_truncated_image_data():
    data = image_bytes(Image.new("RGB", (64, 64)), format="BMP")
    with pytest.raises(ImageProcessingError):
        preprocess_image(data[:len(data) // 2], max_side=1024)


@pytest.mark.parametrize("limit", [100, 200])
def test_oversized_pixel_count_is_rejected(limit, monkeypatch):
    data = image_bytes(Image.new("RGB", (20, 20)))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", limit)
    with pytest.raises(ImageProcessingError, match="limite seguro de pixels"):
        preprocess_image(data, max_side=1024)


def test_exif_orientation_is_applied_and_metadata_is_removed():
    image = Image.new("RGB", (120, 60), "white")
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "private author"
    data = image_bytes(image, format="JPEG", exif=exif)
    with Image.open(BytesIO(preprocess_image(data, max_side=1024))) as prepared:
        assert prepared.size == (60, 120)
        assert not prepared.getexif()


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P"])
def test_transparent_background_becomes_white(mode):
    image = Image.new("RGBA", (120, 60), (0, 0, 0, 0))
    image.paste((0, 0, 0, 255), (0, 0, 30, 60))
    image = image.convert(mode)
    with Image.open(BytesIO(preprocess_image(image_bytes(image), max_side=1024))) as prepared:
        assert prepared.getpixel((100, 30)) > 245
        assert prepared.getpixel((10, 30)) < 10


def test_decode_base64_preserves_original_bytes():
    data = image_bytes(Image.new("RGB", (24, 12)))
    assert decode_image_b64(base64.b64encode(data).decode("ascii")) == data


@pytest.mark.parametrize("value", ["", None, 123, "not-base64", "áááá", "data:image/png;base64,YWJj", "YW Jj", "===="])
def test_invalid_base64_has_controlled_error(value):
    with pytest.raises(ImageProcessingError):
        decode_image_b64(value)


def test_decoded_size_boundary_is_checked_even_with_same_encoded_length(monkeypatch):
    monkeypatch.setattr(imaging, "MAX_IMAGE_BYTES", 4)
    monkeypatch.setattr(imaging, "MAX_IMAGE_B64_CHARS", 8)
    assert decode_image_b64("YWJjZA==") == b"abcd"
    with pytest.raises(ImageTooLargeError):
        decode_image_b64("YWJjZGU=")
    with pytest.raises(ImageTooLargeError):
        decode_image_b64("YWJjZGVmZw==")
