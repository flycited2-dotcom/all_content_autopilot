"""Read-only acceptance check: real vision -> validated intent -> price preview.

No bot, factory command, publication or generation job is invoked by this tool.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from listener.attachments import decode_images
from listener.chatgpt import ChatGPTProvider
from listener.chatgpt_auth import ChatGPTAuth
from listener.intent_validation import validated_intent
from listener.routing import instructions
from orchestrator_bot.supplier_import import prepare_items, validate_source


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("photos", nargs="+", type=Path)
    parser.add_argument("--instruction", required=True, help="Owner's actual price/stock/markup instruction")
    parser.add_argument("--output", type=Path, default=ROOT / "runs/supplier-photo-preview")
    parser.add_argument('--inventory-only', action='store_true')
    args = parser.parse_args(argv)
    from PIL import Image
    formats = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    images = []
    for path in args.photos:
        with Image.open(path) as photo:
            mime = formats[photo.format]
        images.append({"mime_type": mime, "data": base64.b64encode(path.read_bytes()).decode("ascii")})
    payload = {"message": {"id": 1, "kind": "photo", "text": args.instruction, "attachments": images},
               "context": {"capabilities": ["supplier_photo_import"], "history": []}}
    verified = decode_images(payload)
    provider = ChatGPTProvider(ChatGPTAuth(root=ROOT))
    if not provider.ready():
        raise ValueError("ChatGPT для проверки ещё не подключён")
    if args.inventory_only:
        from listener.supplier_vision import extract
        from orchestrator_bot.supplier_inventory import render
        inventory = extract(payload, lambda instruction, source: provider.complete(instruction, source, history=()))
        args.output.mkdir(parents=True, exist_ok=True)
        target = args.output / 'inventory.json'
        target.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding='utf-8')
        (args.output / 'inventory.txt').write_text(render(inventory, []), encoding='utf-8')
        print(json.dumps({'read_only': True, 'complete': inventory['complete'], 'positions': len(inventory['rows']),
                          'output': str(target.resolve()), 'warnings': inventory['warnings']}, ensure_ascii=False))
        return 0
    intent = validated_intent(provider.complete(instructions(ROOT), payload))
    preview = []
    for action in intent["actions"]:
        if action["type"] == "enqueue_supplier_products":
            validate_source(action, payload["message"])
            preview.extend(prepare_items(action, "read-only-photo-preview"))
    args.output.mkdir(parents=True, exist_ok=True)
    result = {"read_only": True, "sources": [{"path": str(path.resolve()), "sha256": photo["sha256"]}
                                               for path, photo in zip(args.photos, verified)],
              "intent": intent, "preview": preview}
    target = args.output / "preview.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Проверка распознавания прайса по фото", "", "Только локальное превью: товары не поставлены в генерацию и не опубликованы.",
             "", intent["reply"], "", "| Модель | Входящая, ₽ | Наценка | Продажная, ₽ | Остаток |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for row in preview:
        lines.append(f"| {row['brand']} {row['model']} | {row['cost_price']} | {row['markup_percent']}% | {row['price']} | {row['stock']} |")
    (args.output / "preview.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"read_only": True, "positions": len(preview), "output": str(target.resolve()),
                      "reply": intent["reply"], "action_types": [a["type"] for a in intent["actions"]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
