#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Генерирует .ico иконки для обоих exe — запускается автоматически из
build_exe.bat перед сборкой, вручную вызывать не нужно.
"""
from PIL import Image, ImageDraw, ImageFont


def load_font(size):
    for path in ('C:/Windows/Fonts/seguisb.ttf', 'C:/Windows/Fonts/segoeui.ttf', 'arial.ttf'):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def bubble_icon(bg, letter):
    size = 256
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((10, 18, 246, 190), radius=52, fill=bg)
    d.polygon([(56, 184), (56, 236), (118, 184)], fill=bg)
    font = load_font(112)
    bbox = d.textbbox((0, 0), letter, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((256 - tw) / 2 - bbox[0], (190 - 18 - th) / 2 + 18 - bbox[1]), letter, font=font, fill='#ffffff')
    return img


def save_ico(img, path):
    img.save(path, format='ICO', sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == '__main__':
    save_ico(bubble_icon('#d4172a', 'A'), 'admin_icon.ico')
    save_ico(bubble_icon('#1565c0', 'К'), 'client_icon.ico')
    print('Иконки готовы: admin_icon.ico, client_icon.ico')
