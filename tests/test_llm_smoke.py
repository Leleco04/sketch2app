"""Manual: uma única tentativa real. Não é executada pelo pytest."""

import argparse
import asyncio
import html
import logging
import os
import shutil
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.imaging import ImageProcessingError, preprocess_image
from app.llm import LLMError, LLMSettings, generate_app


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test Gemini: no máximo uma chamada real, sem webcam.")
    parser.add_argument("image", type=Path, help="Imagem local de um desenho")
    parser.add_argument("--prompt", default="", help="Instruções opcionais de estilo")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        # Carrega o .env antes do pré-processamento para respeitar MAX_IMAGE_SIDE.
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
        if LLMSettings.from_env().mock:
            raise LLMError(
                "mock_enabled", "O smoke test exige a API real. Defina MOCK_LLM=false no .env antes de executá-lo."
            )
        image = preprocess_image(args.image)
        result = asyncio.run(generate_app(image, args.prompt, allow_retries=False))
    except (LLMError, ImageProcessingError) as error:
        print(str(error), file=sys.stderr)
        if isinstance(error, LLMError) and error.hint:
            print(error.hint, file=sys.stderr)
        return 1

    output = ROOT / "generated" / "smoke" / uuid4().hex
    (output / "vendor").mkdir(parents=True)
    shutil.copyfile(ROOT / "web/vendor/tailwind.js", output / "vendor/tailwind.js")
    preview = (
        '<!DOCTYPE html><html lang="pt-BR"><meta charset="utf-8">'
        '<title>Sketch2App — teste Gemini</title>'
        '<body style="margin:0"><iframe title="App gerado" sandbox="allow-scripts" '
        'style="width:100%;height:100vh;border:0" srcdoc="'
        + html.escape(result.html, quote=True) + '"></iframe></body></html>'
    )
    (output / "index.html").write_text(preview, encoding="utf-8")
    print(result.summary)
    print("Prévia salva com iframe sandbox. Para visualizar, execute:")
    prefix = "& " if os.name == "nt" else ""
    print(f'{prefix}"{sys.executable}" -m http.server 8001 --bind 127.0.0.1 --directory "{output}"')
    print("Depois abra http://127.0.0.1:8001. Tailwind será servido localmente.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
