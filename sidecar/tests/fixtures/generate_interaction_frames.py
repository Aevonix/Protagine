"""Synthetic camera frames without text, hidden image metadata or oracle code."""
from pathlib import Path
from PIL import Image, ImageDraw


def main():
    output = Path(__file__).resolve().parents[2]/'protagine/qualification/fixtures'
    for name, centers in [('a', [(140, 225), (350, 225), (560, 225)]), ('b', [(350, 225)])]:
        frame = Image.new('RGB', (720, 450), (245, 245, 245))
        draw = ImageDraw.Draw(frame)
        draw.rectangle((45, 80, 675, 370), fill=(220, 214, 195), outline=(60, 60, 60), width=5)
        for x, y in centers:
            draw.ellipse((x-48, y-48, x+48, y+48), fill=(25, 80, 220), outline=(0, 0, 0), width=3)
        frame.save(output/f'interaction-frame-{name}.png')


if __name__ == '__main__':
    main()
