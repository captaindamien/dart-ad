#!/usr/bin/env python3
"""
Вырезать эталон-маркер из снимка записи (shots/*.png).

    python3 tools/make_marker.py ~/rec/<ts>/shots/0012_t0084.5_video.png \
        --out cand/online.png [--y0 0 --y1 180] [--x0 0 --x1 1920]

По умолчанию — геометрия marker2.png: верхние 180 px на всю ширину. Снимок
должен быть полного разрешения (1920x1080): эталон сравнивается с кадром карты
захвата после общего уменьшения, и масштаб обязан совпадать.
"""

import argparse
import os

import cv2


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("shot")
    ap.add_argument("--out", required=True)
    ap.add_argument("--y0", type=int, default=0)
    ap.add_argument("--y1", type=int, default=180)
    ap.add_argument("--x0", type=int, default=0)
    ap.add_argument("--x1", type=int, default=None)
    a = ap.parse_args()

    img = cv2.imread(a.shot)
    if img is None:
        raise SystemExit(f"не читается: {a.shot}")
    h, w = img.shape[:2]
    if (w, h) != (1920, 1080):
        print(f"[warn] снимок {w}x{h}, а не 1920x1080 — эталон не совпадёт по масштабу с картой захвата")
    x1 = a.x1 if a.x1 is not None else w
    crop = img[a.y0:a.y1, a.x0:x1]
    if crop.size == 0:
        raise SystemExit("пустая область")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    cv2.imwrite(a.out, crop, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    print(f"{a.out}: {crop.shape[1]}x{crop.shape[0]} из {os.path.basename(a.shot)} "
          f"[y {a.y0}:{a.y1}, x {a.x0}:{x1}]")


if __name__ == "__main__":
    main()
