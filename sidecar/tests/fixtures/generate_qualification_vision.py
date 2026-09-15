"""Rebuild the neutral packaged vision fixtures; never derives their oracles."""
from pathlib import Path

from PIL import Image, ImageDraw


# A small fixed bitmap alphabet keeps the labels independent of installed fonts.
GLYPHS = {
    'B': ('11110', '10001', '10001', '11110', '10001', '10001', '11110'),
    'K': ('10001', '10010', '10100', '11000', '10100', '10010', '10001'),
    'M': ('10001', '11011', '10101', '10101', '10001', '10001', '10001'),
    'N': ('10001', '11001', '10101', '10011', '10001', '10001', '10001'),
    'P': ('11110', '10001', '10001', '11110', '10000', '10000', '10000'),
    'Q': ('01110', '10001', '10001', '10001', '10101', '10010', '01101'),
    'R': ('11110', '10001', '10001', '11110', '10100', '10010', '10001'),
    'T': ('11111', '00100', '00100', '00100', '00100', '00100', '00100'),
    '2': ('01110', '10001', '00001', '00010', '00100', '01000', '11111'),
    '3': ('11110', '00001', '00001', '01110', '00001', '00001', '11110'),
    '4': ('00010', '00110', '01010', '10010', '11111', '00010', '00010'),
    '5': ('11111', '10000', '10000', '11110', '00001', '00001', '11110'),
    '6': ('01110', '10000', '10000', '11110', '10001', '10001', '01110'),
    '7': ('11111', '00001', '00010', '00100', '01000', '01000', '01000'),
    '8': ('01110', '10001', '10001', '01110', '10001', '10001', '01110'),
    '9': ('01110', '10001', '10001', '01111', '00001', '00001', '01110'),
}


def label(draw, text, x, y, scale=9):
    for index, char in enumerate(text):
        for row, pixels in enumerate(GLYPHS[char]):
            for column, filled in enumerate(pixels):
                if filled == '1':
                    left = x + (index * 6 + column) * scale
                    top = y + row * scale
                    draw.rectangle((left, top, left + scale - 1, top + scale - 1), fill='black')


def main():
    target = Path(__file__).resolve().parents[2] / 'protagine/qualification/fixtures'
    target.mkdir(exist_ok=True)

    image = Image.new('RGB', (720, 480), 'white')
    draw = ImageDraw.Draw(image)
    draw.ellipse((520, 40, 640, 160), fill=(220, 35, 35))
    draw.polygon(((100, 295), (35, 420), (165, 420)), fill=(30, 80, 220))
    draw.rectangle((515, 315, 645, 445), fill=(245, 210, 20))
    draw.polygon(((40, 120), (115, 55), (115, 95), (230, 95),
                  (230, 145), (115, 145), (115, 185)), fill='black')
    image.save(target / 'vision-arrangement.png', optimize=False)

    image = Image.new('RGB', (720, 420), 'white')
    draw = ImageDraw.Draw(image)
    for y, text in [(35, 'R7K2'), (175, 'M4Q9'), (315, 'B6T3')]:
        draw.rectangle((180, y - 15, 540, y + 78), fill=(238, 244, 250), outline=(80, 80, 80), width=2)
        label(draw, text, 256, y)
    image.save(target / 'vision-labels.png', optimize=False)

    image = Image.new('RGB', (720, 420), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((180, 35, 540, 155), fill=(238, 244, 250), outline=(80, 80, 80), width=2)
    label(draw, 'P8N5', 256, 65)
    # Opaque cover: no hidden letters, metadata or answer-bearing text exist.
    draw.rectangle((180, 240, 540, 360), fill=(95, 95, 95), outline=(35, 35, 35), width=4)
    image.save(target / 'vision-covered-label.png', optimize=False)


if __name__ == '__main__':
    main()
