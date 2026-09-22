"""Render logical Arabic text with OpenType marks on Windows and Linux.

HarfBuzz positions the original Unicode letters and diacritics; FreeType
rasterizes the resulting glyphs. No Quran characters are stripped or reshaped
into presentation-form text before layout.
"""
from functools import lru_cache
from pathlib import Path
import re

import freetype
import uharfbuzz as hb
from PIL import Image, ImageChops


class ArabicText:
    def __init__(self, font_path):
        self.face = hb.Face(Path(font_path).read_bytes())
        self.raster = freetype.Face(str(font_path))

    @lru_cache(maxsize=256)
    def mask(self, text, size):
        font = hb.Font(self.face)
        font.scale = (size * 64, size * 64)
        hb.ot_font_set_funcs(font)
        self.raster.set_pixel_sizes(0, size)
        # Arabic card labels may contain verse numbers or Latin reciter names.
        # Preserve the internal order of these LTR spans within the RTL line.
        runs = [part for part in re.split(r'([A-Za-z0-9٠-٩]+(?:[ ._-][A-Za-z0-9٠-٩]+)*)', text) if part]
        glyphs, pen = [], 0
        for run in reversed(runs):
            buffer = hb.Buffer()
            buffer.add_str(run)
            buffer.guess_segment_properties()
            buffer.direction = 'ltr' if re.fullmatch(r'[A-Za-z0-9٠-٩ ._-]+', run) else 'rtl'
            hb.shape(font, buffer)
            for info, pos in zip(buffer.glyph_infos, buffer.glyph_positions):
                if info.codepoint == 0:
                    raise ValueError('The Arabic font cannot display a character; refusing incomplete Quran text')
                self.raster.load_glyph(info.codepoint, freetype.FT_LOAD_RENDER)
                slot = self.raster.glyph
                bitmap = slot.bitmap
                if bitmap.width and bitmap.rows:
                    mask = Image.frombytes('L', (bitmap.width, bitmap.rows), bytes(bitmap.buffer),
                                           'raw', 'L', abs(bitmap.pitch), 1 if bitmap.pitch > 0 else -1)
                    x = round((pen + pos.x_offset) / 64) + slot.bitmap_left
                    y = -round(pos.y_offset / 64) - slot.bitmap_top
                    glyphs.append((x, y, mask))
                pen += pos.x_advance
        if not glyphs:
            return Image.new('L', (max(1, round(pen / 64)), 1))
        left = min(0, min(x for x, _, _ in glyphs))
        top = min(y for _, y, _ in glyphs)
        right = max(round(pen / 64), max(x + mask.width for x, _, mask in glyphs))
        bottom = max(y + mask.height for _, y, mask in glyphs)
        result = Image.new('L', (right - left, bottom - top))
        for x, y, mask in glyphs:
            box = (x - left, y - top, x - left + mask.width, y - top + mask.height)
            result.paste(ImageChops.lighter(result.crop(box), mask), box)
        return result

    def draw_centered(self, image, text, y, size, color, width=840):
        mask = self.mask(text, size)
        while mask.width > width and size > 18:
            size -= 2
            mask = self.mask(text, size)
        if mask.width > width or y + mask.height > image.height:
            raise ValueError('Complete Arabic text does not fit on the card')
        image.paste(color, ((image.width - mask.width) // 2, y), mask)

    def wrap(self, text, size, width=800):
        lines, current = [], ''
        for word in text.split():
            candidate = (current + ' ' + word).strip()
            if current and self.mask(candidate, size).width > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        return lines
