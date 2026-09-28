"""Render the adopter's compact GPU-run path helper graphic.

This is a source-controlled, code-rendered explanatory diagram. It is not an
image-generation substitute for narrative imagery; its role is a labeled
architecture helper graphic.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "content-system-assets" / "Vast GPU Broker architecture.png"
SIZE = (1600, 650)

INK = "#14253D"
MUTED = "#53657B"
BLUE = "#2868A8"
TEAL = "#258477"
AMBER = "#BD7726"
PALE_BLUE = "#EAF2FA"
PALE_TEAL = "#E9F5F2"
PALE_AMBER = "#FBF2E7"
LINE = "#CBD6E2"
WHITE = "#FFFFFF"
CANVAS = "#F6F8FB"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf")
    if path.is_file():
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def centered(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, face: ImageFont.ImageFont, fill: str) -> None:
    draw.text(xy, text, font=face, fill=fill, anchor="mm")


def render() -> Path:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", SIZE, CANVAS)
    draw = ImageDraw.Draw(image)

    # Header: the title deliberately describes a planned flow rather than
    # implying that every lifecycle step is already shipped.
    draw.text((92, 75), "Planned GPU run path", font=font(44, True), fill=INK)
    draw.text(
        (94, 132),
        "Evidence, live prices and owner limits guide every paid step",
        font=font(23),
        fill=MUTED,
    )
    draw.rounded_rectangle((1270, 74, 1507, 120), radius=20, fill=PALE_AMBER)
    centered(draw, (1388, 97), "TARGET WORKFLOW", font(15, True), AMBER)

    cards = [
        (78, 252, 334, 500, BLUE, PALE_BLUE, "1", "Research", "Resolve the exact model", "and deployment evidence"),
        (378, 252, 634, 500, BLUE, PALE_BLUE, "2", "Quote", "Search current offers", "with requested disk costs"),
        (678, 252, 934, 500, AMBER, PALE_AMBER, "3", "Check limits", "Apply owner spend, time", "and network boundaries"),
        (978, 252, 1234, 500, TEAL, PALE_TEAL, "4", "Run bounded", "Create only after gates;", "monitor the lease"),
        (1278, 252, 1534, 500, TEAL, PALE_TEAL, "5", "Verify cleanup", "Destroy on completion;", "confirm it is gone"),
    ]

    for left, top, right, bottom, accent, tint, number, title, line1, line2 in cards:
        draw.rounded_rectangle((left, top, right, bottom), radius=18, fill=WHITE, outline=LINE, width=2)
        draw.rounded_rectangle((left, top, right, top + 10), radius=5, fill=accent)
        draw.ellipse((left + 24, top + 25, left + 70, top + 71), fill=tint)
        centered(draw, (left + 47, top + 48), number, font(21, True), accent)
        draw.text((left + 24, top + 104), title, font=font(25, True), fill=INK)
        draw.text((left + 24, top + 158), line1, font=font(16), fill=MUTED)
        draw.text((left + 24, top + 185), line2, font=font(16), fill=MUTED)

    # Directional links are outside the cards so each stage stays legible.
    for x in (351, 651, 951, 1251):
        mid = 376
        draw.line((x - 11, mid, x + 11, mid), fill=LINE, width=4)
        draw.polygon(((x + 13, mid), (x + 3, mid - 7), (x + 3, mid + 7)), fill=LINE)

    draw.rounded_rectangle((78, 548, 1534, 598), radius=16, fill="#EEF2F6")
    centered(
        draw,
        (806, 573),
        "If evidence or limits are missing, stop for research or owner input before creating anything",
        font(17, True),
        INK,
    )

    image.save(OUTPUT, format="PNG", optimize=False)
    return OUTPUT


if __name__ == "__main__":
    print(render())
