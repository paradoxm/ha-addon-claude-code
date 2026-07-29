#!/usr/bin/env python3
"""Generate the add-on's icon.png and logo.png.

Deliberately original artwork rather than Anthropic's marks: this is an
unofficial add-on in a public repository, and Anthropic's branding guidance asks
third-party products to keep their own branding and to avoid visual elements
that mimic Claude Code.

The motif is a shell prompt — a chevron and a block cursor — in the same warm
graphite and amber as the web UI. Drawn at 4x and downsampled, because Pillow's
shape drawing is not antialiased.

Usage: python3 tools/make-icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "claude-code"
SCALE = 4

GROUND = (29, 24, 21)
RULE = (74, 61, 49)
AMBER = (229, 161, 60)
DIM = (110, 100, 88)


def rounded(size, radius):
    """A rounded panel with a hairline border, matching the UI's surfaces."""
    image = Image.new("RGBA", (size[0] * SCALE, size[1] * SCALE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        [0, 0, image.width - 1, image.height - 1],
        radius=radius * SCALE,
        fill=GROUND,
        outline=RULE,
        width=1 * SCALE,
    )
    return image, draw


def stroke_segment(draw, start, end, thickness, colour):
    """One stroke with round caps, drawn as a quad plus a disc at each end.

    Pillow's `line(joint="curve")` leaves a blunt, uneven corner where two thick
    strokes meet, which at icon size reads as a cross rather than a chevron.
    """
    (x0, y0), (x1, y1) = start, end
    length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    if not length:
        return
    # Normal to the segment, scaled to half the stroke width.
    nx = -(y1 - y0) / length * thickness / 2
    ny = (x1 - x0) / length * thickness / 2
    draw.polygon(
        [
            (x0 + nx, y0 + ny),
            (x1 + nx, y1 + ny),
            (x1 - nx, y1 - ny),
            (x0 - nx, y0 - ny),
        ],
        fill=colour,
    )
    for x, y in (start, end):
        radius = thickness / 2
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=colour)


def prompt_motif(draw, left, mid, cap, stroke, gap, cursor_w):
    """A '>' followed by a block cursor. Returns the right edge it drew to.

    `cap` is the full height of both the chevron and the cursor, so the two read
    as one line of type rather than as two unrelated shapes.
    """
    half = cap / 2
    # A pointed chevron: a shallow angle turns into a blob once stroked.
    apex = left + cap * 0.64
    stroke_segment(draw, (left, mid - half), (apex, mid), stroke, AMBER)
    stroke_segment(draw, (apex, mid), (left, mid + half), stroke, AMBER)

    cursor_left = apex + gap
    draw.rounded_rectangle(
        [cursor_left, mid - half, cursor_left + cursor_w, mid + half],
        radius=stroke / 3,
        fill=AMBER,
    )
    return cursor_left + cursor_w


def make_icon():
    size = 128
    image, draw = rounded((size, size), 22)
    mid = image.height / 2

    cap = 44 * SCALE
    stroke = 7 * SCALE
    gap = 14 * SCALE
    cursor_w = 15 * SCALE

    # Centre the whole motif rather than positioning each piece by hand.
    width = cap * 0.64 + stroke + gap + cursor_w
    left = (image.width - width) / 2 + stroke / 2
    prompt_motif(draw, left, mid, cap, stroke, gap, cursor_w)

    out = image.resize((size, size), Image.LANCZOS)
    out.save(OUT / "icon.png")
    return out.size


def make_logo():
    image, draw = rounded((250, 100), 14)
    mid = image.height / 2

    cap = 36 * SCALE
    right = prompt_motif(draw, 24 * SCALE, mid, cap, 6 * SCALE, 11 * SCALE, 13 * SCALE)

    # Hairlines standing in for output: the same instrument look as the web UI.
    start = right + 18 * SCALE
    for index, length in enumerate((146, 104, 126)):
        y = mid + (index - 1) * 15 * SCALE
        draw.line(
            [(start, y), (start + length * SCALE, y)],
            fill=DIM if index == 1 else RULE,
            width=2 * SCALE,
        )

    out = image.resize((250, 100), Image.LANCZOS)
    out.save(OUT / "logo.png")
    return out.size


if __name__ == "__main__":
    print("icon.png", make_icon())
    print("logo.png", make_logo())
