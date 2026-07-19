#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inspect_debug.py — печатает структуру JSON-ответа (пути ключей, длины
списков, количество вхождений) БЕЗ значений, чтобы не светить содержимое
писем. Плюс немного статистики по mid/tid/fid.

Использование:
  python3 inspect_debug.py dump/_debug/folder_8_0.json
  python3 inspect_debug.py dump/_debug/thread_*.json
"""
import collections
import glob
import json
import signal
import sys

try:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # дружим с | head
except (AttributeError, ValueError):
    pass


def inspect(path):
    js = json.load(open(path, encoding="utf-8"))
    paths = collections.Counter()
    list_lens = {}
    mids, tids, fids = set(), set(), collections.Counter()
    mid_dict_keys = collections.Counter()

    def walk(o, p=""):
        if isinstance(o, dict):
            if "mid" in o:
                mids.add(str(o["mid"]))
                for k in o:
                    mid_dict_keys[k] += 1
                if o.get("fid") is not None:
                    fids[str(o["fid"])] += 1
            for k, v in o.items():
                if k in ("tid", "threadId", "thread_id") and isinstance(v, str):
                    tids.add(v)
                walk(v, p + "." + k)
        elif isinstance(o, list):
            key = p + "[]"
            list_lens[key] = max(list_lens.get(key, 0), len(o))
            for v in o:
                walk(v, key)
        else:
            paths[p] += 1

    walk(js)
    print("=" * 70)
    print("Файл:", path)
    print("-- пути (лист-узлы, сколько раз встретились):")
    for p, c in sorted(paths.items()):
        print("   %-60s %d" % (p, c))
    print("-- списки (максимальная длина):")
    for p, l in sorted(list_lens.items()):
        print("   %-60s %d" % (p, l))
    print("-- статистика:")
    print("   уникальных mid:", len(mids))
    print("   уникальных tid:", len(tids))
    print("   fid у писем:", dict(fids))
    print("   ключи в словарях с mid:", dict(mid_dict_keys))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for arg in sys.argv[1:]:
        for path in sorted(glob.glob(arg)):
            inspect(path)
