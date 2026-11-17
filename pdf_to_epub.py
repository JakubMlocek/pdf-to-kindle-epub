#!/usr/bin/env python3
"""Convert text PDFs to reflowable EPUB; optional book-specific layout profiles."""

import argparse
from collections import Counter
import html
import io
import json
import logging
from pathlib import Path
import posixpath
import re
import tempfile
import uuid
import zipfile

import fitz
from ebooklib import epub
from lxml import etree
from PIL import Image

LOG = logging.getLogger("pdf_to_epub")
CSS = """
body { margin: 0; line-height: 1.35; }
p { margin: 0 0 .65em; text-indent: 0; }
h1 { font-size: 1.7em; margin: 1em 0 .8em; }
h2 { font-size: 1.45em; margin: 1em 0 .6em; }
h3 { font-size: 1.2em; margin: .9em 0 .5em; }
h4 { font-size: 1.06em; margin: .8em 0 .4em; }
img { max-width: 100%; height: auto; }
figure { margin: .9em 0; padding: 0; text-align: center; break-inside: avoid; }
.list { margin-left: 1em; text-indent: -.6em; }
code { font-family: monospace; font-size: .9em; overflow-wrap: anywhere; }
"""
DEFAULT_PROFILE = {
    "top_margin": 0,
    "bottom_margin": 0,
    "heading_font": "",
    "body_font": "",
    "heading_sizes": None,
    "body_min_size": 0,
    "diagram_fonts": [],
    "snapshot_pages": "",
    "cover_page": None,
}


def clean(text):
    # The source uses both '-\xad' for a compound and plain '\xad'.
    # Normalize those locally; literal '--' in code is intentionally untouched.
    text = text.replace("-\xad", "-").replace("\xad", "-")
    for old, new in {
        "\xa0": " ", "\u2003": " ", "\u2002": " ", "\ufeff": "",
        "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
    }.items():
        text = text.replace(old, new)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def text_of(block):
    return "".join(s["text"] for line in block.get("lines", []) for s in line["spans"])


def spans_of(block):
    return [s for line in block.get("lines", []) for s in line["spans"]]


def page_numbers(spec, total):
    """Parse one-based inclusive ranges, rejecting mistakes instead of ignoring them."""
    result = set()
    if not spec:
        return result
    for part in spec.split(","):
        match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", part)
        if not match:
            raise ValueError(f"Invalid page range: {part!r}")
        first = int(match[1])
        last = int(match[2] or match[1])
        if first < 1 or last < first or last > total:
            raise ValueError(f"Page range {part!r} is outside 1-{total}")
        result.update(range(first - 1, last))
    return result


def span_html(span, drop_last_hyphen=False):
    if "ZapfDing" in span["font"]:
        return ""
    value = clean(span["text"])
    if drop_last_hyphen:
        value = re.sub(r"-\s*$", "", value)
    value = html.escape(value.replace("\t", " "))
    font = span["font"]
    if "Bold" in font or "Black" in font:
        value = f"<strong>{value}</strong>"
    if "Italic" in font or "Obl" in font:
        value = f"<em>{value}</em>"
    if "Code" in font or "Mono" in font:
        value = f"<code>{value}</code>"
    return value


def block_html(block, profile, heading_sizes):
    lines = [line for line in block["lines"] if line["spans"]]
    spans = [s for s in spans_of(block) if s["text"].strip() and "Zapf" not in s["font"]]
    if not spans:
        return ""
    maximum = max(s["size"] for s in spans)
    heading_font = profile["heading_font"]
    heading = maximum >= heading_sizes[-1] and (
        all(heading_font in s["font"] for s in spans) if heading_font
        else all(s["size"] >= heading_sizes[-1] for s in spans)
    )
    if heading:
        level = next((i + 1 for i, size in enumerate(heading_sizes) if maximum >= size), 4)
        # Join heading lines explicitly: text_of() concatenates PDF lines without spaces.
        title = " ".join(clean("".join(s["text"] for s in line["spans"])) for line in lines)
        return f"<h{level}>{html.escape(re.sub(r'\s+', ' ', title).strip())}</h{level}>"
    bullet = any("ZapfDing" in s["font"] for s in spans_of(block))
    groups, current = [], []
    for line in lines:
        if current:
            previous = current[-1]
            indent = line["bbox"][0] - block["bbox"][0]
            gap = line["bbox"][1] - previous["bbox"][3]
            if (indent >= 8 and previous["bbox"][2] > line["bbox"][0] + 45 and not bullet) or gap > 5:
                groups.append(current)
                current = []
        current.append(line)
    if current:
        groups.append(current)
    output = []
    for group in groups:
        pieces = []
        for index, line in enumerate(group):
            raw = "".join(s["text"] for s in line["spans"]).rstrip()
            next_raw = "".join(s["text"] for s in group[index + 1]["spans"]).lstrip() if index + 1 < len(group) else ""
            joins_word = bool(next_raw and re.match(r"^[a-z]", next_raw) and raw.endswith(("-", "\xad")))
            # '-\xad' marks a compound; a plain final '-' is treated as a wrap.
            drop_hyphen = joins_word and raw.endswith("-") and not raw.endswith("--")
            nonempty = [i for i, s in enumerate(line["spans"]) if s["text"].strip()]
            final_span = nonempty[-1] if nonempty else -1
            pieces.append("".join(span_html(s, drop_hyphen and i == final_span) for i, s in enumerate(line["spans"])))
            if index + 1 < len(group):
                pieces.append("" if joins_word else " ")
        value = "".join(pieces).strip()
        output.append((f'<p class="list">• {value}</p>' if bullet else f"<p>{value}</p>"))
    return "\n".join(output)


def merge_zones(zones):
    # Preserve the original script's vertical grouping for multi-part diagrams.
    result = []
    for rect in sorted(zones, key=lambda r: r.y0):
        if result and rect.y0 <= result[-1].y1 + 5:
            result[-1] |= rect
        else:
            result.append(fitz.Rect(rect))
    return result


def graphic_zones(page, blocks, profile, bounds, heading_sizes, tables=True):
    zones = []
    for block in blocks:
        rect = fitz.Rect(block["bbox"])
        if block["type"] == 1 and rect.width > 12 and rect.height > 12:
            zones.append(rect)
        elif block["type"] == 0 and any(
            font in span["font"] for span in spans_of(block) for font in profile["diagram_fonts"]
        ):
            zones.append(rect)
    for block in blocks:
        if block["type"] != 0 or not re.match(r"FIGURE\d", re.sub(r"\s+", "", text_of(block)), re.I):
            continue
        rect = fitz.Rect(block["bbox"])
        below = []
        for candidate in blocks:
            if candidate["type"] or candidate["bbox"][1] < rect.y1 + 2:
                continue
            if any(
                (profile["body_font"] and profile["body_font"] in s["font"] and s["size"] >= profile["body_min_size"])
                or (s["size"] >= heading_sizes[-1])
                or (not profile["body_font"] and not profile["diagram_fonts"])
                for s in spans_of(candidate)
            ):
                below.append(candidate["bbox"][1])
        bottom = min(below) - 8 if below else bounds.y1
        if bottom > rect.y1 + 12:
            zones.append(fitz.Rect(bounds.x0, rect.y0, bounds.x1, bottom))
    if tables:
        for table in page.find_tables().tables:
            rect = fitz.Rect(table.bbox)
            if table.row_count >= 2 and table.col_count >= 2 and rect.width > 100 and rect.height > 35:
                zones.append(rect & bounds)
    zones = [rect & bounds for rect in zones if not (rect & bounds).is_empty]
    # Include an intersected text block in full so no prose disappears at image edges.
    while True:
        zones = merge_zones(zones)
        changed = False
        for index, zone in enumerate(zones):
            for block in blocks:
                rect = fitz.Rect(block["bbox"]) & bounds
                if zone.intersects(rect):
                    expanded = zone | rect
                    # MuPDF rounds rectangle unions to float32. Compare with a
                    # tolerance instead of repeatedly trying to contain float64
                    # text coordinates that differ by a few millionths of a point.
                    if any(abs(a - b) > 0.01 for a, b in zip(expanded, zone)):
                        zone = expanded
                        zones[index] = expanded
                        changed = True
        if not changed:
            break
    return [(rect + (-2, -2, 2, 2)) & bounds for rect in zones]


def validate_epub(path):
    """Structural checks; this is not a replacement for EPUBCheck or visual review."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    with zipfile.ZipFile(path) as archive:
        if archive.namelist()[0] != "mimetype" or archive.read("mimetype") != b"application/epub+zip":
            raise ValueError("Invalid EPUB mimetype")
        if archive.getinfo("mimetype").compress_type != zipfile.ZIP_STORED:
            raise ValueError("EPUB mimetype must be stored without compression")
        names = set(archive.namelist())
        trees = {n: etree.fromstring(archive.read(n), parser) for n in names if n.endswith((".xml", ".xhtml", ".opf", ".ncx"))}
        ids = {n: set(tree.xpath("//@id")) for n, tree in trees.items()}
        for name, tree in trees.items():
            for ref in tree.xpath("//@href | //@src"):
                if re.match(r"^[a-zA-Z][\w+.-]*:", ref) or ref.startswith("//"):
                    continue
                target, _, fragment = ref.partition("#")
                target = posixpath.normpath(posixpath.join(posixpath.dirname(name), target)) if target else name
                if target not in names:
                    raise ValueError(f"Missing EPUB resource: {name} -> {ref}")
                if fragment and fragment not in ids.get(target, set()):
                    raise ValueError(f"Missing EPUB anchor: {name} -> {ref}")


class Converter:
    def __init__(self, args, profile):
        self.args, self.profile = args, profile
        self.book = epub.EpubBook()
        self.image_count = 0

    def snapshot(self, page, rect, label):
        self.image_count += 1
        name = f"images/figure-{self.image_count:04}.jpg"
        pixmap = page.get_pixmap(matrix=fitz.Matrix(self.args.image_scale, self.args.image_scale), clip=rect, alpha=False)
        image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
        data = io.BytesIO()
        image.save(data, "JPEG", quality=90, optimize=True)
        self.book.add_item(epub.EpubItem(uid=f"image-{self.image_count}", file_name=name, media_type="image/jpeg", content=data.getvalue()))
        return f'<figure><img src="{name}" alt="{html.escape(label, quote=True)}" /></figure>'

    def convert(self, document, output):
        args, profile, book = self.args, self.profile, self.book
        if document.needs_pass:
            raise ValueError("Password-protected PDFs are not supported")
        if not len(document):
            raise ValueError("The PDF has no pages")
        if profile.get("expected_pages") and len(document) != profile["expected_pages"]:
            raise ValueError(f"The selected profile expects {profile['expected_pages']} pages, but the PDF has {len(document)}.")
        snapshots = page_numbers(args.snapshot_pages if args.snapshot_pages is not None else profile["snapshot_pages"], len(document))
        cover_page = args.cover_page if args.cover_page is not None else profile["cover_page"]
        if cover_page is not None and not 1 <= cover_page <= len(document):
            raise ValueError("Cover page is outside the PDF")
        metadata = document.metadata or {}
        title = args.title or metadata.get("title") or args.input.stem
        book.set_identifier("urn:uuid:" + str(uuid.uuid4()))
        book.set_title(clean(title))
        book.set_language(args.language)
        authors = args.author or ([metadata["author"]] if metadata.get("author") else [])
        for author in authors:
            book.add_author(clean(author))
        style = epub.EpubItem(uid="style", file_name="style.css", media_type="text/css", content=CSS.encode())
        book.add_item(style)
        if cover_page is not None:
            pixmap = document[cover_page - 1].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            book.set_cover("cover.jpg", pixmap.pil_tobytes(format="JPEG", quality=92), create_page=False)
        toc = [entry for entry in document.get_toc() if 1 <= entry[2] <= len(document)]
        titles = {}
        for level, label, page in toc:
            if level == 1:
                titles.setdefault(page - 1, clean(label))
        starts = sorted({0, *titles})
        sections = []
        sizes = Counter()
        if profile["heading_sizes"] is None:
            for number in range(min(20, len(document))):
                page = document[number]
                for block in page.get_text("dict")["blocks"]:
                    for span in spans_of(block):
                        sizes[round(span["size"], 1)] += len(span["text"])
        body_size = sizes.most_common(1)[0][0] if sizes else 9.5
        heading_sizes = profile["heading_sizes"] or [body_size * 2.4, body_size * 1.8, body_size * 1.4, body_size * 1.2]
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(document)
            section_title = titles.get(start, clean(title))
            content = []
            for number in range(start, end):
                page = document[number]
                content.append(f'<span id="p{number + 1}"></span>')
                if number == (cover_page - 1 if cover_page is not None else -1):
                    content.append('<figure><img src="cover.jpg" alt="Cover" /></figure>')
                    continue
                top = args.top_margin if args.top_margin is not None else profile["top_margin"]
                bottom = args.bottom_margin if args.bottom_margin is not None else profile["bottom_margin"]
                bounds = fitz.Rect(page.rect.x0, page.rect.y0 + top, page.rect.x1, page.rect.y1 - bottom)
                if bounds.is_empty:
                    raise ValueError(f"Margins exclude the whole page {number + 1}")
                if number in snapshots:
                    content.append(self.snapshot(page, bounds, f"Source PDF page {number + 1}"))
                    continue
                # Extract with the clipping region first, so partially intersecting text blocks survive.
                blocks = page.get_text("dict", clip=bounds)["blocks"]
                zones = graphic_zones(page, blocks, profile, bounds, heading_sizes, tables=not args.no_tables)
                events = [(zone.y0, zone.x0, self.snapshot(page, zone, f"Diagram or table, PDF page {number + 1}")) for zone in zones]
                for block in blocks:
                    if block["type"] or any((zone + (-0.01, -0.01, 0.01, 0.01)).contains(fitz.Rect(block["bbox"]) & bounds) for zone in zones):
                        continue
                    events.append((block["bbox"][1], block["bbox"][0], block_html(block, profile, heading_sizes)))
                content.extend(event[2] for event in sorted(events))
            section = epub.EpubHtml(title=section_title, file_name=f"section-{index:03}.xhtml", lang=args.language)
            section.content = "\n".join(content)
            section.add_item(style)
            book.add_item(section)
            sections.append((start, end, section))
            LOG.info("%s (pages %s-%s)", section_title, start + 1, end)
        navigation, stack = [], []
        stack.append((0, navigation))
        for index, (level, label, page) in enumerate(toc):
            section = next(section for start, end, section in sections if start <= page - 1 < end)
            link = epub.Link(f"{section.file_name}#p{page}", clean(label), f"toc-{index}")
            while len(stack) > 1 and stack[-1][0] >= level:
                stack.pop()
            children = []
            stack[-1][1].append((link, children))
            stack.append((level, children))

        def nested(nodes):
            return [(link, nested(children)) if children else link for link, children in nodes]

        book.toc = nested(navigation) if navigation else [section for _, _, section in sections]
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ["nav"] + [section for _, _, section in sections]
        epub.write_epub(str(output), book, {"epub3_landmark": False, "epub3_pages": False})
        validate_epub(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Source PDF (text layer required for reflow)")
    parser.add_argument("output", type=Path, help="Destination .epub")
    parser.add_argument("--profile", default="generic", help="generic or a JSON profile path")
    parser.add_argument("--title")
    parser.add_argument("--author", action="append", help="Repeat for multiple authors")
    parser.add_argument("--language", default="en", help="Language tag, e.g. en or pl")
    parser.add_argument("--snapshot-pages", help="Keep selected pages as images, e.g. 3-4,10")
    parser.add_argument("--cover-page", type=int, help="One-based page number used as the cover")
    parser.add_argument("--top-margin", type=float, help="Exclude this many PDF points from the top")
    parser.add_argument("--bottom-margin", type=float, help="Exclude this many PDF points from the bottom")
    parser.add_argument("--image-scale", type=float, default=3, help="Image render scale; default 3 (216 dpi)")
    parser.add_argument("--no-tables", action="store_true", help="Disable ruled-table detection")
    parser.add_argument("--force", action="store_true", help="Replace an existing EPUB")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.output.suffix.lower() != ".epub":
            raise ValueError("Output filename must end with .epub")
        if args.input.resolve() == args.output.resolve():
            raise ValueError("Input and output paths must differ")
        if args.output.exists() and not args.force:
            raise ValueError("Output already exists; use --force to replace it")
        if not 0 < args.image_scale <= 6:
            raise ValueError("Image scale must be greater than 0 and at most 6")
        if any(value is not None and value < 0 for value in (args.top_margin, args.bottom_margin)):
            raise ValueError("Margins cannot be negative")
        profile = dict(DEFAULT_PROFILE)
        if args.profile != "generic":
            path = Path(args.profile)
            custom = json.loads(path.read_text(encoding="utf-8"))
            unknown = custom.keys() - (DEFAULT_PROFILE.keys() | {"name", "expected_pages"})
            if unknown:
                raise ValueError(f"Unknown profile settings: {', '.join(sorted(unknown))}")
            profile.update(custom)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with fitz.open(args.input) as document:
            with tempfile.TemporaryDirectory(prefix="pdf-to-epub-", dir=args.output.parent) as directory:
                temporary = Path(directory) / "result.epub"
                converter = Converter(args, profile)
                converter.convert(document, temporary)
                temporary.replace(args.output)
        LOG.info("Saved %s (%0.1f MB, %s illustrations); structural validation passed.", args.output, args.output.stat().st_size / 1_000_000, converter.image_count)
    except (ValueError, OSError, RuntimeError, json.JSONDecodeError, fitz.FileDataError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
