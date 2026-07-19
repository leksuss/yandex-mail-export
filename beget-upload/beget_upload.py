#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
beget_upload.py — заливка выгруженных .eml на почтовый сервер Beget
через IMAP APPEND с сохранением папок, флага прочитанности и дат писем.

Только стандартная библиотека, ничего ставить не нужно.

Использование:
  python3 beget_upload.py --dump dump --user box@ваш-домен.ру
  (пароль спросит интерактивно; либо переменная окружения IMAP_PASS)

Полезное:
  --dry-run       показать, что и куда поедет, без заливки
  --folder-map    'Рассылки=INBOX' — переопределить назначение папки
  --host/--port   по умолчанию imap.beget.com:993 (проверьте в панели Beget)

Скрипт резюмируемый: список уже залитых писем хранится в
<dump>/uploaded_<user>.txt — повторный запуск дольёт только недостающее.
"""

import argparse
import base64
import email
import email.utils
import getpass
import imaplib
import json
import os
import re
import sys
import time

imaplib._MAXLINE = 10 * 1024 * 1024

# Стандартные папки Яндекса -> типовые папки Dovecot (Beget)
SPECIAL = {
    "inbox": "INBOX", "входящие": "INBOX",
    "sent": "Sent", "отправленные": "Sent",
    "drafts": "Drafts", "черновики": "Drafts",
    "spam": "Spam", "спам": "Spam", "junk": "Spam",
    "trash": "Trash", "удалённые": "Trash", "удаленные": "Trash",
    "deleted": "Trash",
    "archive": "Archive", "архив": "Archive",
    "outbox": "Sent", "исходящие": "Sent",
}


def imap_utf7_encode(s):
    """Кодирование имён папок в modified UTF-7 (RFC 3501)."""
    out, buf = [], []

    def flush():
        if buf:
            b = "".join(buf).encode("utf-16-be")
            b64 = base64.b64encode(b).decode().rstrip("=").replace("/", ",")
            out.append("&" + b64 + "-")
            buf.clear()

    for ch in s:
        o = ord(ch)
        if 0x20 <= o <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def get_delimiter(m):
    typ, data = m.list('""', '""')
    if typ == "OK" and data and data[0]:
        line = data[0].decode(errors="replace")
        mm = re.search(r'\)\s+"(.)"\s+', line)
        if mm:
            return mm.group(1)
    return "."  # Dovecot по умолчанию


def existing_mailboxes(m):
    boxes = set()
    typ, data = m.list()
    if typ == "OK":
        for line in data or []:
            if not line:
                continue
            s = line.decode(errors="replace")
            mm = re.search(r'(?:"([^"]+)"|(\S+))\s*$', s)
            if mm:
                boxes.add(mm.group(1) or mm.group(2))
    return boxes


def target_mailbox(folder_name, delimiter, overrides):
    if folder_name in overrides:
        return overrides[folder_name]
    key = folder_name.strip().lower()
    if key in SPECIAL:
        return SPECIAL[key]
    # пользовательская папка: вложенность Яндекса "A|B" -> "A<delim>B"
    parts = re.split(r"[|/]", folder_name)
    return delimiter.join(imap_utf7_encode(p.strip()) for p in parts if p.strip())


def msg_timestamp(entry, raw):
    ts = entry.get("timestamp")
    if ts:
        return float(ts)
    try:
        msg = email.message_from_bytes(raw)
        dt = email.utils.parsedate_to_datetime(msg.get("Date"))
        if dt:
            return dt.timestamp()
    except Exception:
        pass
    return time.time()


def fetch_existing_message_ids(m, box):
    """Message-ID всех писем, уже лежащих в данной папке на сервере
    (без скачивания тела — только заголовок, батчами)."""
    typ, _ = m.select('"%s"' % box.replace('"', '\\"'), readonly=True)
    if typ != "OK":
        return set()
    typ, data = m.search(None, "ALL")
    if typ != "OK" or not data or not data[0]:
        return set()
    ids = data[0].split()
    result = set()
    batch = 300
    for i in range(0, len(ids), batch):
        chunk = b",".join(ids[i:i + batch]).decode()
        typ, resp = m.fetch(chunk, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        if typ != "OK":
            continue
        for item in resp:
            if isinstance(item, tuple) and item[1]:
                msg = email.message_from_bytes(item[1])
                mid = msg.get("Message-ID")
                if mid:
                    result.add(mid.strip())
    return result


def extract_message_id(raw):
    try:
        return (email.message_from_bytes(raw).get("Message-ID") or "").strip()
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser(description="Заливка .eml на IMAP Beget")
    ap.add_argument("--dump", required=True, help="каталог с выгрузкой")
    ap.add_argument("--user", required=True, help="почтовый ящик (логин IMAP)")
    ap.add_argument("--host", default="imap.beget.com")
    ap.add_argument("--port", type=int, default=993)
    ap.add_argument("--delay", type=float, default=0.05)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--folder-map", action="append", default=[],
                    help="Переопределение: 'ИмяПапкиЯндекса=ПапкаНаBeget'")
    ap.add_argument("--dedupe", action="store_true",
                    help="пропускать письма, чей Message-ID уже есть в "
                         "целевой папке на сервере (медленнее на старте, "
                         "зато без дублей при пересечении с уже имеющейся "
                         "на Beget почтой)")
    args = ap.parse_args()

    manifest_path = os.path.join(args.dump, "manifest.jsonl")
    if not os.path.exists(manifest_path):
        sys.exit("Нет %s — сначала запустите yandex_dump.py" % manifest_path)

    entries = []
    with open(manifest_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    # дедупликация по mid (манифест может содержать повторы после резюмов)
    entries = list({e["mid"]: e for e in entries}.values())
    print("Писем в манифесте: %d" % len(entries))

    overrides = {}
    for spec in args.folder_map:
        if "=" in spec:
            k, v = spec.split("=", 1)
            overrides[k] = v

    state_path = os.path.join(
        args.dump, "uploaded_%s.txt" % re.sub(r"\W+", "_", args.user))
    uploaded = set()
    if os.path.exists(state_path):
        uploaded = set(open(state_path, encoding="utf-8").read().split())
        print("Уже залито ранее: %d" % len(uploaded))

    if args.dry_run:
        plan = {}
        for e in entries:
            plan.setdefault(e["folder"], [0, 0])
            plan[e["folder"]][0] += 1
            if e["mid"] in uploaded:
                plan[e["folder"]][1] += 1
        for fld, (n, done) in plan.items():
            print("  %-30s -> %-20s  %d писем (%d уже залито)"
                  % (fld, target_mailbox(fld, ".", overrides), n, done))
        return

    password = os.environ.get("IMAP_PASS") or getpass.getpass(
        "Пароль от %s: " % args.user)

    m = imaplib.IMAP4_SSL(args.host, args.port)
    typ, _ = m.login(args.user, password)
    if typ != "OK":
        sys.exit("Логин не удался")
    delim = get_delimiter(m)
    boxes = existing_mailboxes(m)
    print("Подключено к %s, разделитель папок: %r" % (args.host, delim))

    state = open(state_path, "a", encoding="utf-8")
    ok = fail = dup = 0
    created = set()
    existing_ids_by_box = {}   # box -> set(Message-ID) на сервере, лениво

    for e in entries:
        if e["mid"] in uploaded:
            continue
        path = os.path.join(args.dump, e["path"])
        if not os.path.exists(path):
            print("  ! файла нет: %s" % path)
            fail += 1
            continue
        raw = open(path, "rb").read()
        box = target_mailbox(e["folder"], delim, overrides)
        if box not in boxes and box not in created and box.upper() != "INBOX":
            m.create(box)          # "already exists" молча игнорируем
            m.subscribe(box)
            created.add(box)

        if args.dedupe:
            if box not in existing_ids_by_box:
                print("  читаю Message-ID уже существующих писем в %s..." % box)
                existing_ids_by_box[box] = fetch_existing_message_ids(m, box)
                print("    там уже %d писем" % len(existing_ids_by_box[box]))
            mid_hdr = extract_message_id(raw)
            if mid_hdr and mid_hdr in existing_ids_by_box[box]:
                dup += 1
                state.write(e["mid"] + "\n")   # чтобы не проверять повторно
                state.flush()
                continue

        flags = None if e.get("unread") else r"(\Seen)"
        idate = imaplib.Time2Internaldate(msg_timestamp(e, raw))
        try:
            typ, resp = m.append(box, flags, idate, raw)
        except imaplib.IMAP4.abort:
            # переподключение при обрыве
            m = imaplib.IMAP4_SSL(args.host, args.port)
            m.login(args.user, password)
            typ, resp = m.append(box, flags, idate, raw)
        if typ == "OK":
            ok += 1
            state.write(e["mid"] + "\n")
            state.flush()
            if args.dedupe and extract_message_id(raw):
                existing_ids_by_box[box].add(extract_message_id(raw))
            if ok % 50 == 0:
                print("  ...%d залито" % ok)
        else:
            fail += 1
            print("  ! APPEND fail mid=%s: %s" % (e["mid"], resp))
        time.sleep(args.delay)

    m.logout()
    state.close()
    print("\nГотово: залито %d, пропущено дублей %d, ошибок %d."
          % (ok, dup, fail))
    if fail:
        print("Повторный запуск дольёт неудавшиеся (резюмирование по %s)."
              % state_path)


if __name__ == "__main__":
    main()
