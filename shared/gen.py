#!/usr/bin/env python3
"""Общий генератор для проектов Станислава (Gemini). Ключ берётся из models.env.

Использование (любой агент проекта, работает от root):
  python3 /opt/friend-claude/shared/gen.py image "промпт" out.png [--edit input.png]
  python3 /opt/friend-claude/shared/gen.py html  "описание страницы" out.html
  python3 /opt/friend-claude/shared/gen.py ask   "вопрос/задача"        # текст
  python3 /opt/friend-claude/shared/gen.py video "промпт" out.mp4 [--fast]

Модели: картинки gemini-2.5-flash-image (Nano Banana), HTML/сайты gemini-pro-latest,
текст gemini-flash-latest, видео Veo.
Тарифицируется на Google-аккаунте (общий ключ). Секрет ключа наружу не выводится.
"""
import sys, os, re, json, base64, time, urllib.request
from pathlib import Path

KEYS_FILE = Path("/root/.config/friend-claude/models.env")
BASE = "https://generativelanguage.googleapis.com/v1beta"
IMG_MODEL = "gemini-2.5-flash-image"
TXT_MODEL = "gemini-flash-latest"
DESIGN_MODEL = "gemini-pro-latest"      # сайты/HTML (сильнее)
VEO = {"full": "veo-3.1-generate-preview", "fast": "veo-3.1-fast-generate-preview"}


def key():
    for line in KEYS_FILE.read_text().splitlines():
        if line.startswith("GEMINI_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("нет GEMINI_API_KEY в models.env")


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=180).read())


def gen_image(prompt, out, edit=None):
    parts = [{"text": prompt}]
    if edit and Path(edit).is_file():
        data = base64.b64encode(Path(edit).read_bytes()).decode()
        mime = "image/png" if edit.lower().endswith("png") else "image/jpeg"
        parts.insert(0, {"inlineData": {"mimeType": mime, "data": data}})
    r = _post(f"{BASE}/models/{IMG_MODEL}:generateContent?key={key()}", {"contents": [{"parts": parts}]})
    for p in r.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        inl = p.get("inlineData") or p.get("inline_data")
        if inl and inl.get("data"):
            Path(out).write_bytes(base64.b64decode(inl["data"]))
            print(f"OK image -> {out} ({Path(out).stat().st_size//1024} КБ)")
            return
    raise SystemExit("картинка не вернулась: " + json.dumps(r)[:300])


def ask(prompt):
    r = _post(f"{BASE}/models/{TXT_MODEL}:generateContent?key={key()}", {"contents": [{"parts": [{"text": prompt}]}]})
    txt = "".join(p.get("text", "") for p in r.get("candidates", [{}])[0].get("content", {}).get("parts", []))
    print(txt.strip() or "(пусто)")


def gen_html(prompt, out):
    sp = ("Сгенерируй ПОЛНУЮ самодостаточную HTML-страницу по описанию. Один файл: инлайн CSS "
          "(и JS, если нужно), без внешних зависимостей и CDN, адаптивно (мобилка+десктоп), "
          "аккуратная типографика. Верни ТОЛЬКО HTML-код, без пояснений и без markdown-ограждений.\n\n"
          "Описание: " + prompt)
    r = _post(f"{BASE}/models/{DESIGN_MODEL}:generateContent?key={key()}", {"contents": [{"parts": [{"text": sp}]}]})
    txt = "".join(p.get("text", "") for p in r.get("candidates", [{}])[0].get("content", {}).get("parts", [])).strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\n", "", txt)
        txt = re.sub(r"\n```\s*$", "", txt)
    if not txt:
        raise SystemExit("HTML не вернулся")
    Path(out).write_text(txt, encoding="utf-8")
    print(f"OK html -> {out} ({len(txt)} символов)")


def gen_video(prompt, out, fast=True):
    model = VEO["fast" if fast else "full"]
    op = _post(f"{BASE}/models/{model}:predictLongRunning?key={key()}",
               {"instances": [{"prompt": prompt}], "parameters": {"aspectRatio": "16:9"}})
    name = op.get("name")
    if not name:
        raise SystemExit("видео-операция не создалась: " + json.dumps(op)[:300])
    print(f"видео генерится (операция {name.split('/')[-1][:12]}…), жду…")
    for _ in range(60):
        time.sleep(10)
        st = json.loads(urllib.request.urlopen(f"{BASE}/{name}?key={key()}", timeout=60).read())
        if st.get("done"):
            resp = st.get("response", {})
            vids = resp.get("generateVideoResponse", {}).get("generatedSamples") or resp.get("videos") or []
            uri = None
            if vids:
                uri = (vids[0].get("video") or {}).get("uri") or vids[0].get("uri")
            if not uri:
                raise SystemExit("видео готово, но URI не найден: " + json.dumps(st)[:400])
            data = urllib.request.urlopen(f"{uri}&key={key()}" if "?" in uri else f"{uri}?key={key()}", timeout=180).read()
            Path(out).write_bytes(data)
            print(f"OK video -> {out} ({len(data)//1024} КБ)")
            return
    raise SystemExit("видео не готово за отведённое время")


def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "image":
        edit = None
        if "--edit" in sys.argv:
            edit = sys.argv[sys.argv.index("--edit") + 1]
        gen_image(sys.argv[2], sys.argv[3], edit)
    elif cmd == "ask":
        ask(sys.argv[2])
    elif cmd == "html":
        gen_html(sys.argv[2], sys.argv[3])
    elif cmd == "video":
        gen_video(sys.argv[2], sys.argv[3], fast=("--fast" in sys.argv or "--full" not in sys.argv))
    else:
        print("неизвестная команда:", cmd); print(__doc__); sys.exit(1)


if __name__ == "__main__":
    main()
