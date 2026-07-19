#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yandex_dump.py — выгрузка всех писем из Яндекс.Почты БЕЗ IMAP,
через внутренний веб-API (тот же, которым пользуется mail.yandex.ru).

Скрипт обращается ТОЛЬКО к mail.yandex.ru (см. константу BASE ниже) —
ваши cookies никуда больше не отправляются.

Требуется: python3, requests  (pip install requests)

Подготовка (см. README.md для подробностей со скриншотами):
  1. Залогиньтесь в нужный ящик на https://mail.yandex.ru в браузере.
  2. DevTools (F12) -> Network -> кликните любое письмо -> выберите любой
     запрос к mail.yandex.ru -> Headers -> скопируйте ЗНАЧЕНИЕ заголовка
     Cookie целиком в файл cookies.txt (одной строкой).
     (Поддерживается и формат Netscape cookies.txt из расширений браузера.)

Использование:
  python3 yandex_dump.py --cookies cookies.txt --out dump --list-folders
  python3 yandex_dump.py --cookies cookies.txt --out dump --probe
  python3 yandex_dump.py --cookies cookies.txt --out dump

Результат:
  dump/<Папка>/<mid>.eml
  dump/manifest.jsonl   (mid, папка, unread, timestamp — для заливки)

Если Яндекс поменял API — запустите с --debug: сырые JSON-ответы лягут в
dump/_debug/, по ним легко поправить регулярки/параметры. В частности,
если --probe не находит письмо, посмотрите в вебе URL кнопки «Свойства
письма → Скачать оригинал» и передайте его через --source-template; если
папки/письма не листятся вовсе — вероятно, у вас другое имя внутренней
модели API, см. --api-model.
"""

import argparse
import json
import os
import re
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("Нужен пакет requests:  pip install requests")

BASE = "https://mail.yandex.ru"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Возможные шаблоны URL «Скачать оригинал письма». Скрипт пробует по очереди
# и запоминает первый сработавший. Если ни один не подошёл — откройте письмо
# в вебе, нажмите «Свойства письма → Скачать оригинал», подсмотрите URL в
# DevTools и передайте его шаблоном через --source-template
# (плейсхолдеры: {base} {mid} {uid} {model}).
SOURCE_TEMPLATES = [
    "{base}/web-api/message-source/{model}/{uid}/{mid}/yandex_email.eml",
    "{base}/message_source/{mid}?_uid={uid}&name=message.eml",
    "{base}/message_source/{mid}",
    "{base}/lite/message_source/{mid}",
    "{base}/u{uid}/message_source/{mid}",
]

RFC822_RE = re.compile(
    r"(?mi)^(received|return-path|delivered-to|from|to|subject|date|message-id):"
)


# ---------------------------------------------------------------- session ---

def load_cookies(path):
    raw = open(path, "r", encoding="utf-8", errors="replace").read().strip()
    jar = {}
    if raw.startswith("# Netscape") or "\t" in raw.splitlines()[0]:
        for line in raw.splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                jar[parts[5]] = parts[6]
    else:
        # сырая строка заголовка Cookie
        for kv in raw.split(";"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                jar[k.strip()] = v.strip()
    if not jar:
        sys.exit("Не смог разобрать cookies из " + path)
    return jar


def make_session(cookies):
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "ru,en;q=0.9",
        "Origin": BASE,
        "Referer": BASE + "/",
    })
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=".yandex.ru")
    return s


def bootstrap(sess, args):
    """Достаём кандидатов ckey (CSRF) и uid со страницы почты."""
    if args.ckey and args.uid:
        return [args.ckey], args.uid
    r = sess.get(BASE + "/", allow_redirects=True, timeout=30)
    html = r.text
    if args.debug:
        _dbg_write(args, "bootstrap.html", html)
    if "passport.yandex" in r.url:
        sys.exit("Редирект на паспорт: cookies не подхватились или протухли. "
                 "Перелогиньтесь в браузере и скопируйте Cookie заново.")
    ckeys = [args.ckey] if args.ckey else []
    for v in _find_ckeys(html):
        if v not in ckeys:
            ckeys.append(v)
    uid = args.uid
    if not uid:
        m = re.search(r'"uid"\s*:\s*"?(\d{6,})"?', html)
        if m:
            uid = m.group(1)
    if not ckeys:
        sys.exit("Не нашёл ckey на странице. Возьмите его из DevTools: любой "
                 "POST на /web-api/models/... содержит поле _ckey. "
                 "Передайте через --ckey (и --uid).")
    if not uid:
        print("! uid не найден автоматически — некоторые шаблоны URL могут "
              "не сработать. Можно передать через --uid.")
        uid = "0"
    return ckeys, uid


# ----------------------------------------------------------------- models ---

def call_models(sess, ckey, models_and_params, debug_tag=None, args=None):
    """POST /web-api/models/<api_model> — универсальный вызов внутренних
    моделей веб-интерфейса. Имя <api_model> (по умолчанию 'liza1') видно
    в DevTools в URL любого POST-запроса на /web-api/models/... — если у
    вас оно другое, передайте через --api-model."""
    api_model = (args.api_model if args and getattr(args, "api_model", None)
                else "liza1")
    data = {"_ckey": ckey}
    names = []
    for i, (name, params) in enumerate(models_and_params):
        names.append(name)
        data["_model.%d" % i] = name
        for k, v in params.items():
            data["%s.%d" % (k, i)] = v
    url = BASE + "/web-api/models/%s?_m=%s" % (api_model, ",".join(names))
    last_exc = None
    for attempt in range(4):
        try:
            r = sess.post(url, data=data, timeout=60)
            r.raise_for_status()
            js = r.json()
            break
        except (requests.RequestException, ValueError) as e:
            last_exc = e
            time.sleep(1.5 * (attempt + 1))
    else:
        raise last_exc
    if args and args.debug and debug_tag:
        _dbg_write(args, debug_tag + ".json", json.dumps(js, ensure_ascii=False, indent=1))
    return js


def iter_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_dicts(v)


def _unescape_json_str(v):
    """'\\u002FJt..' -> '/Jt..', '\\/..' -> '/..'"""
    v = v.replace("\\/", "/")
    return re.sub(r"\\u([0-9a-fA-F]{4})",
                  lambda m: chr(int(m.group(1), 16)), v)


def _find_ckeys(html):
    out = []
    for m in re.finditer(r'"ckey"\s*:\s*"([^"]+)"', html):
        v = _unescape_json_str(m.group(1))
        if v not in out:
            out.append(v)
    return out


def refresh_ckeys(sess):
    """Свежие кандидаты ckey со страницы почты."""
    try:
        html = sess.get(BASE + "/", timeout=30).text
        return _find_ckeys(html)
    except requests.RequestException:
        return []


def is_ckey_error(js):
    return any(isinstance(d, dict) and d.get("error") == "ckey"
               for d in iter_dicts(js))


def get_folders(sess, ckeys, args, rounds=3):
    """Перебираем кандидатов ckey; при неудаче берём свежие со страницы.
    Возвращает (folders, рабочий_ckey)."""
    js = None
    good_ckey = None
    for rnd in range(rounds):
        for ck in list(ckeys):
            js = call_models(sess, ck, [("folders", {})],
                             "folders_r%d" % rnd, args)
            if any(d.get("fid") is not None and "mid" not in d
                   for d in iter_dicts(js)):
                good_ckey = ck
                break
            snippet = json.dumps(js, ensure_ascii=False)[:300]
            print("  folders: пустой ответ с ckey=%s...: %s"
                  % (ck[:8], snippet))
            time.sleep(1.0)
        if good_ckey:
            break
        # все кандидаты не сработали — перечитываем страницу за свежими
        fresh = refresh_ckeys(sess)
        if fresh:
            ckeys[:] = fresh
            print("  обновил ckey со страницы: %d кандидатов" % len(fresh))
    if not good_ckey:
        sys.exit("Модель folders так и не вернула папок (ответы выше). "
                 "Если в ответе ругань на ckey/token — возьмите живой _ckey "
                 "из DevTools (Network -> любой POST models) и передайте "
                 "через --ckey.")
    folders, seen = [], set()
    for d in iter_dicts(js):
        fid = d.get("fid")
        if fid is None or "mid" in d or fid in seen:
            continue
        name = (d.get("name") or d.get("title") or
                (d.get("symbolicName") or {}).get("title")
                if isinstance(d.get("symbolicName"), dict) else d.get("name"))
        if not name:
            name = d.get("symbol") or ("folder_%s" % fid)
        seen.add(fid)
        folders.append({"fid": str(fid), "name": str(name),
                        "count": d.get("count") or d.get("messagesCount")})
    return folders, good_ckey


def _extract_msgs(js):
    msgs, seen = [], set()
    for d in iter_dicts(js):
        mid = d.get("mid")
        if mid is None or mid in seen:
            continue
        # отсекаем служебные объекты без признаков письма
        if not any(k in d for k in ("fid", "date", "subject", "utc_timestamp",
                                    "subjEmpty", "firstline", "tid")):
            continue
        seen.add(mid)
        ts = d.get("utc_timestamp")
        if ts is None and isinstance(d.get("date"), dict):
            ts = d["date"].get("timestamp")
        try:
            ts = int(ts) if ts is not None else None
            if ts and ts > 10 ** 12:      # миллисекунды -> секунды
                ts //= 1000
        except (TypeError, ValueError):
            ts = None
        status = d.get("status")
        unread = bool(d.get("new")) or (
            isinstance(status, (list, tuple)) and "new" in status)
        tcount = d.get("count")
        if not isinstance(tcount, int):
            tcount = None
        fid = d.get("fid")
        msgs.append({"mid": str(mid), "timestamp": ts, "unread": unread,
                     "tid": d.get("tid"),
                     "fid": str(fid) if fid is not None else None,
                     "tcount": tcount})
    return msgs


def _extract_tids(js):
    """Собираем tid по всему ответу: тред может быть отдельным объектом,
    а не полем внутри словаря письма. Возвращает {tid: count|None}."""
    tids = {}
    for d in iter_dicts(js):
        tid = d.get("tid") or d.get("threadId") or d.get("thread_id")
        if not tid or not isinstance(tid, str):
            continue
        cnt = d.get("count")
        if not isinstance(cnt, int):
            cnt = d.get("threadCount")
            if not isinstance(cnt, int):
                cnt = None
        old = tids.get(tid)
        if old is None or (cnt or 0) > (old or 0):
            tids[tid] = cnt
    return tids


def query_messages(sess, ckey_holder, base_params, first, count, args, tag):
    """Одна страница модели messages (папка или тред) с ретраями и
    автообновлением ckey."""
    params = dict(base_params)
    params.update({
        "first": str(first),
        "last": str(first + count),
        "sort_type": "date1",
        "extra_cond": "",
        "goto": "all",
    })

    def call(ck):
        return call_models(sess, ck, [("messages", params)],
                           "%s_%d" % (tag, first), args)

    js = None
    for attempt in range(4):
        js = call(ckey_holder["ckey"])
        msgs = _extract_msgs(js)
        if msgs:
            return msgs, _extract_tids(js)
        if is_ckey_error(js):
            print("  ckey протух посреди прогона — обновляю...")
            for ck in refresh_ckeys(sess):
                js = call(ck)
                if not is_ckey_error(js):
                    ckey_holder["ckey"] = ck
                    break
            msgs = _extract_msgs(js)
            if msgs:
                return msgs, _extract_tids(js)
        # пустой ответ: либо конец списка, либо флак API — переспросим
        time.sleep(1.0 + attempt)
    return [], {}


def list_folder_heads(sess, ckey_holder, fid, args):
    """Все письма папки + все tid, встреченные в ответах.
    ВАЖНО: сервер отдаёт максимум ~30 писем за запрос независимо от
    first/last, поэтому шаг пагинации = сколько реально пришло.
    Возвращает (heads, {tid: count|None})."""
    out, seen, first = [], set(), 0
    tid_counts = {}
    while True:
        msgs, tids = query_messages(sess, ckey_holder,
                                    {"current_folder": fid},
                                    first, args.page, args,
                                    "folder_%s" % fid)
        for t, c in tids.items():
            old = tid_counts.get(t)
            if old is None or (c or 0) > (old or 0):
                tid_counts[t] = c
        page = [m for m in msgs if m["mid"] not in seen]
        if not page:
            break
        for m in page:
            seen.add(m["mid"])
        out.extend(page)
        first += len(msgs)
        if len(out) % 300 < len(msgs):
            print("  ...листинг: %d" % len(out))
    return out, tid_counts


def list_thread(sess, ckey_holder, tid, args, page=200):
    """Все письма одного треда (учитываем серверный лимит страницы)."""
    out, seen, first = [], set(), 0
    while True:
        got, _ = query_messages(sess, ckey_holder,
                                {"thread_id": tid},
                                first, page, args,
                                "thread_%s" % tid)
        msgs = [m for m in got if m["mid"] not in seen]
        if not msgs:
            break
        for m in msgs:
            seen.add(m["mid"])
        out.extend(msgs)
        first += len(got)
    return out


# ----------------------------------------------------------------- source ---

def looks_like_rfc822(body):
    try:
        head = body[:4096].decode("latin1")
    except Exception:
        return False
    return bool(RFC822_RE.search(head))


def fetch_source(sess, mid, uid, templates, cache, model):
    """Скачивает .eml, перебирая шаблоны URL; удачный кладёт в cache[0]."""
    order = ([cache[0]] if cache[0] else []) + [t for t in templates
                                               if t != cache[0]]
    for tpl in order:
        url = tpl.format(base=BASE, mid=mid, uid=uid, model=model)
        try:
            r = sess.get(url, timeout=60)
        except requests.RequestException:
            continue
        if r.status_code == 200 and looks_like_rfc822(r.content):
            cache[0] = tpl
            return r.content
    return None


# ------------------------------------------------------------------ misc ----

def sanitize(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return name or "folder"


def _dbg_write(args, fname, content):
    d = os.path.join(args.out, "_debug")
    os.makedirs(d, exist_ok=True)
    mode = "w" if isinstance(content, str) else "wb"
    with open(os.path.join(d, fname), mode,
              **({"encoding": "utf-8"} if mode == "w" else {})) as f:
        f.write(content)


# ------------------------------------------------------------------ main ----

def main():
    ap = argparse.ArgumentParser(description="Выгрузка Яндекс.Почты в .eml без IMAP")
    ap.add_argument("--cookies", required=True, help="файл с cookies")
    ap.add_argument("--out", default="dump", help="каталог для выгрузки")
    ap.add_argument("--delay", type=float, default=0.4,
                    help="пауза между скачиваниями, сек (не злите антибота)")
    ap.add_argument("--page", type=int, default=100, help="писем за страницу")
    ap.add_argument("--folders", nargs="*", default=None,
                    help="выгружать только эти папки (по имени)")
    ap.add_argument("--list-folders", action="store_true",
                    help="показать папки и выйти")
    ap.add_argument("--probe", action="store_true",
                    help="скачать одно письмо для проверки и выйти")
    ap.add_argument("--source-template", action="append", default=[],
                    help="свой шаблон URL оригинала письма")
    ap.add_argument("--ckey", help="CSRF-токен вручную (из DevTools)")
    ap.add_argument("--uid", help="uid ящика вручную")
    ap.add_argument("--api-model", default="liza1",
                    help="имя внутренней модели веб-API Яндекс.Почты "
                         "(видно в DevTools в URL POST-запросов на "
                         "/web-api/models/<это>; по умолчанию 'liza1', "
                         "но может отличаться в вашем аккаунте/регионе)")
    ap.add_argument("--debug", action="store_true",
                    help="сохранять сырые ответы API в _debug/")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    sess = make_session(load_cookies(args.cookies))
    ckeys, uid = bootstrap(sess, args)
    print("ckey-кандидатов: %d, uid: %s, api-model: %s"
          % (len(ckeys), uid, args.api_model))

    folders, ckey = get_folders(sess, ckeys, args)
    ckey_holder = {"ckey": ckey}
    print("Папки:")
    for f in folders:
        print("  fid=%-8s %-30s писем: %s" % (f["fid"], f["name"], f["count"]))
    if args.list_folders:
        return
    if args.folders:
        want = {n.lower() for n in args.folders}
        folders = [f for f in folders if f["name"].lower() in want]

    templates = args.source_template + SOURCE_TEMPLATES
    tpl_cache = [None]
    manifest_path = os.path.join(args.out, "manifest.jsonl")
    done_mids = set()
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            for line in f:
                try:
                    done_mids.add(json.loads(line)["mid"])
                except Exception:
                    pass
        print("Резюмируем: уже выгружено %d писем" % len(done_mids))

    manifest = open(manifest_path, "a", encoding="utf-8")
    total = errors = 0

    for f in folders:
        fdir = os.path.join(args.out, sanitize(f["name"]))
        os.makedirs(fdir, exist_ok=True)

        # 1. Полный листинг папки (в тредном режиме это шапки тредов).
        heads, tid_counts = list_folder_heads(sess, ckey_holder, f["fid"], args)
        expected = f["count"]
        print("Папка «%s»: в листинге %d элементов, счётчик папки: %s, "
              "тредов замечено: %d"
              % (f["name"], len(heads), expected, len(tid_counts)))

        # 2. Если элементов меньше счётчика — листинг тредный,
        #    разворачиваем цепочки и добираем остальные письма папки.
        work = {m["mid"]: m for m in heads}
        need_expand = expected is None or len(heads) < int(expected)
        if need_expand:
            for m in heads:   # tid из словарей писем тоже учитываем
                if m.get("tid") and m["tid"] not in tid_counts:
                    tid_counts[m["tid"]] = m.get("tcount")
            # поле count в ответе ненадёжно (у всех тредов равно 1),
            # поэтому разворачиваем все треды без фильтра
            tids = list(tid_counts)
            if not tids and expected and len(heads) < int(expected):
                print("  ! НЕДОБОР, но ни одного tid в ответах не найдено.\n"
                      "    Запустите с --debug --folders «%s» и пришлите "
                      "файл dump/_debug/folder_%s_0.json — надо смотреть "
                      "структуру ответа." % (f["name"], f["fid"]))
            if tids:
                print("  разворачиваю %d тредов..." % len(tids))
            for i, tid in enumerate(tids, 1):
                for tm in list_thread(sess, ckey_holder, tid, args):
                    if tm["mid"] in work:
                        continue
                    # письмо треда из другой папки заберём при её обходе
                    if tm.get("fid") and tm["fid"] != str(f["fid"]):
                        continue
                    work[tm["mid"]] = tm
                if i % 200 == 0:
                    print("  ...тредов развёрнуто: %d/%d, писем в папке: %d"
                          % (i, len(tids), len(work)))
                time.sleep(args.delay / 4)
            print("  после разворачивания: %d писем" % len(work))

        # 3. Скачивание.
        for m in work.values():
            if m["mid"] in done_mids:
                continue
            body = fetch_source(sess, m["mid"], uid, templates, tpl_cache,
                                args.api_model)
            if body is None:
                errors += 1
                print("  ! не скачалось mid=%s (папка %s)"
                      % (m["mid"], f["name"]))
                if args.probe:
                    sys.exit("Probe: ни один шаблон URL не сработал. "
                             "Подсмотрите URL «Скачать оригинал» в DevTools "
                             "и передайте через --source-template")
                continue
            path = os.path.join(fdir, m["mid"] + ".eml")
            with open(path, "wb") as fh:
                fh.write(body)
            manifest.write(json.dumps({
                "mid": m["mid"], "folder": f["name"],
                "unread": m["unread"], "timestamp": m["timestamp"],
                "path": os.path.relpath(path, args.out),
            }, ensure_ascii=False) + "\n")
            manifest.flush()
            done_mids.add(m["mid"])
            total += 1
            if args.probe:
                print("Probe OK: %s (%d байт), шаблон: %s"
                      % (path, len(body), tpl_cache[0]))
                return
            if total % 50 == 0:
                print("  ...%d писем" % total)
            time.sleep(args.delay)
        print("Папка «%s» готова." % f["name"])

    manifest.close()
    print("\nИтого скачано: %d, ошибок: %d. Каталог: %s" %
          (total, errors, args.out))
    if errors:
        print("Ошибочные mid можно добить повторным запуском (резюмирование "
              "по manifest.jsonl).")


if __name__ == "__main__":
    main()
