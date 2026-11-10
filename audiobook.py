"""Turn a scanned PDF book into an MP3 audiobook.

Stages (each one caches its output in the work dir, so re-runs skip finished work):
  1. ocr   - extract each page's scan and OCR it with tesseract (in parallel)  -> pages/NNNN.txt
  2. text  - drop running headers, footnotes, captions and scan noise; rejoin paragraphs;
             split into ~30 minute parts at section headings                  -> text/NN.txt, book.txt
  3. tts   - synthesize each part with edge-tts (online, Microsoft voices)    -> audio/NN - Title.mp3

Usage:
  python3 audiobook.py ~/Downloads/History_Of_Georgia.pdf --first-page 4 --last-page 508
  python3 audiobook.py BOOK.pdf --stage text         # re-run one stage
  python3 audiobook.py BOOK.pdf --voice en-GB-RyanNeural
"""
import argparse
import asyncio
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DIR = Path(__file__).resolve().parent


def page_count(pdf: Path) -> int:
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    raise RuntimeError(f"could not read page count of {pdf}")


def ocr_page(pdf: Path, page: int, out: Path, lang: str, dpi: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "p"
        # Scanned books embed one image per page: OCR it as-is. Re-rendering it at another
        # resolution resamples the scan and made tesseract drop word fragments.
        subprocess.run(["pdfimages", "-png", "-f", str(page), "-l", str(page), str(pdf), str(prefix)], check=True)
        images = sorted(Path(tmp).glob("p-*.png"))
        if len(images) != 1:
            subprocess.run(["pdftoppm", "-r", str(dpi), "-png", "-singlefile",
                            "-f", str(page), "-l", str(page), str(pdf), str(prefix)], check=True)
            images = [Path(f"{prefix}.png")]
        text = subprocess.run(
            ["tesseract", str(images[0]), "-", "-l", lang],
            capture_output=True, text=True, check=True,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        ).stdout
    tmp_out = out.with_suffix(".part")
    tmp_out.write_text(text, encoding="utf8")
    tmp_out.rename(out)


def run_ocr(pdf: Path, work: Path, lang: str, dpi: int, jobs: int) -> None:
    pages_dir = work / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    total = page_count(pdf)
    todo = [p for p in range(1, total + 1) if not (pages_dir / f"{p:04d}.txt").exists()]
    print(f"OCR: {total} pages, {total - len(todo)} cached, {len(todo)} to do ({jobs} jobs)")
    with ThreadPoolExecutor(jobs) as pool:
        futures = {pool.submit(ocr_page, pdf, p, pages_dir / f"{p:04d}.txt", lang, dpi): p for p in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            fut.result()
            print(f"  [{i}/{len(todo)}] page {futures[fut]}", flush=True)


# ---------------------------------------------------------------- text cleanup

HEADERS: list[str] = []  # running headers (book title, author), found by find_headers()
# A footnote starts with *, ** or a superscript mark (OCR'd as ' or ’) followed by text.
FOOTNOTE_RE = re.compile(r"^\s*(\*{1,3}|[¹²³'’‘])\s*[A-Z\"“]")
WORD_RE = re.compile(r"[A-Za-z]+")
SENTENCE_END = tuple('.!?:;"”)')
# Short words hunspell's base list lacks, plus a whitelist for the start of a paragraph.
COMMON = set(
    "a an the of in on at to by for from as is are was were be been it its this that these those "
    "and or but not no with which who whom whose he she they we his her their our i you if after "
    "before during when while since until".split()
)
# Drop caps (the big first letter of a section) are lost in OCR: "n 1991" was "In 1991".
DROP_CAP_FIXES = {"n": "In", "s": "As", "t": "It", "f": "Of", "he": "The", "ihe": "The", "tbe": "The",
                  "fhe": "The", "rhe": "The", "thc": "The", "fs": "The"}


WORDS: set[str] = set()  # every dictionary word, lowercased
LOWER_WORDS: set[str] = set()  # only words the dictionary spells lowercase (no proper nouns)


def load_words() -> None:
    for name in ("en_US", "en_GB"):
        dic = Path(f"/usr/share/hunspell/{name}.dic")
        if dic.exists():
            for line in dic.read_text(encoding="utf8", errors="ignore").splitlines()[1:]:
                w = line.split("/")[0]
                WORDS.add(w.lower())
                if w.islower():
                    LOWER_WORDS.add(w)
    if not WORDS:
        sys.exit("no hunspell English dictionary found in /usr/share/hunspell")
    WORDS.update(COMMON)
    LOWER_WORDS.update(COMMON)


def is_word(w: str, common: bool = False) -> bool:
    """Dictionary lookup with crude suffix stripping; common=True rejects proper nouns."""
    words = LOWER_WORDS if common else WORDS
    w = w.lower()
    if w in words:
        return len(w) > 1 or w in ("a", "i")
    return any(w.endswith(s) and len(w) - len(s) >= 3 and w[: -len(s)] in words
               for s in ("s", "es", "ed", "d", "ing", "ly"))


def word_ratio(text: str, caps_ok: bool = True) -> float:
    toks = WORD_RE.findall(text)
    good = sum(is_word(t) and (caps_ok or len(t) == 1 or not t.isupper()) for t in toks)
    return good / len(toks) if toks else 0.0


def norm(line: str) -> str:
    return re.sub(r"[^a-z]", "", line.lower())


def find_headers(pages: dict[int, str]) -> list[str]:
    """Running headers: short lines found near the top of at least 10% of pages."""
    counts = Counter()
    for text in pages.values():
        top = [ln for ln in text.splitlines() if ln.strip()][:4]
        counts.update({norm(ln) for ln in top if len(ln) < 50 and len(norm(ln)) >= 8})
    return [h for h, n in counts.items() if n >= len(pages) / 10]


def is_header(line: str) -> bool:
    return len(line) < 50 and any(h in norm(line) for h in HEADERS)


def page_blocks(text: str) -> list[list[str]]:
    """Split a page into blank-line-separated blocks, dropping headers, junk, page numbers and footnotes."""
    blocks, cur = [], []
    for line in text.splitlines():
        if line.strip():
            cur.append(line.strip())
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)

    # running header (sometimes below or beside OCR noise from the header ornament)
    for i, block in enumerate(blocks[:3]):
        block[:] = [ln for ln in block[:3] if not is_header(ln)] + block[3:]
        if i < 2:  # ornament noise glued to the top of the first text block
            while block and len(block[0]) < LONG_LINE and word_ratio(block[0], caps_ok=False) < 0.75:
                block.pop(0)

    parts = [part for b in blocks if b for part in split_heading(b)]
    total_lines = sum(map(len, parts))
    last_line = next((part[-1] for part in reversed(parts) if WORD_RE.search(" ".join(part))), "")
    kept = []
    for i, part in enumerate(parts):
        joined = " ".join(part)
        below = sum(map(len, parts[i:]))
        if FOOTNOTE_RE.match(part[0]) and kept and below <= max(8, total_lines * 0.3) \
                and last_line.endswith((*SENTENCE_END, *"0123456789")):
            return kept  # footnotes run to the bottom of the page (body text would run on to the next)
        good = [t for t in WORD_RE.findall(joined) if is_word(t)]
        if any(len(line) >= LONG_LINE for line in part):
            if word_ratio(joined) >= 0.5:
                kept.append(part)  # body text
        elif word_ratio(joined, caps_ok=False) >= 0.75 and sum(len(t) >= 4 for t in good) >= 2:
            kept.append(part)  # heading or short line of text; the rest is map/ornament noise
    return kept


LONG_LINE = 60  # body text lines are ~85 chars; headings and noise are shorter


def split_heading(block: list[str]) -> list[list[str]]:
    """Split leading short heading lines off a block of body text."""
    i = 0
    while i < len(block) - 1 and len(block[i]) < LONG_LINE and block[i][:1].isupper():
        i += 1
    if 0 < i < len(block) and len(block[i]) >= LONG_LINE:
        return [block[:i], block[i:]]
    return [block]


def join_lines(lines: list[str]) -> str:
    out = ""
    for line in lines:
        if not out:
            out = line
        elif out.endswith("-") and line[:1].islower():
            head, tail = re.search(r"(\w+)-$", out), re.match(r"\w+", line)
            joined = (head.group(1) if head else "") + (tail.group(0) if tail else "")
            out = out[:-1] + line if is_word(joined) else out + line
        else:
            out += " " + line
    return out


def fix_drop_cap(par: str) -> str:
    """Repair the first word of a paragraph whose decorated initial letter OCR lost."""
    m = re.match(r"[^\w(]*([A-Za-z]+)(\s.*)", par, re.S)
    if not m:
        return par  # starts with a digit or an abbreviation like "B.C."
    first, rest = m.group(1), m.group(2)
    if first.lower() in DROP_CAP_FIXES and (first[0].islower() or not is_word(first)):
        return DROP_CAP_FIXES[first.lower()] + rest
    if not re.match(r"\s+[a-z]", rest):
        return par  # "Georgia Is ..." / "fs New Stone Age" handled above; leave capitalized runs alone
    if first[0].islower() and len(first) >= 3 and not is_word(first):
        exact = [c for c in "TAIOWBDHSMFCEGLNPRU" if (c + first).lower() in LOWER_WORDS]
        loose = [c for c in "TAIOWBDHSMFCEGLNPRU" if is_word(c + first, common=True)]
        names = [c for c in "GAIKLCTBDEHJMNOPRSVZ" if (c + first).lower() in WORDS]  # Georgian, Alexander
        for cap in (exact or loose or names)[:1]:
            # the lost letter sometimes lands at the start of the 2nd line: "evolved Das a tribal"
            stray = re.search(rf"\s({cap})([a-z]+)\b", rest[:150])
            if stray and is_word(stray.group(2)) and not is_word(stray.group(0).strip()):
                rest = rest[: stray.start(1)] + rest[stray.end(1):]
            return cap + first + rest
    noise = not is_word(first) or (first[0].isupper() and len(first) <= 2 and first.lower() not in COMMON)
    if noise and len(first) <= 3:
        rest = rest.lstrip()  # OCR debris where the drop cap was ("Ra the sixth..."): drop it
        return rest[:1].upper() + rest[1:]
    return par


def is_heading(block: list[str]) -> bool:
    return all(len(line) < LONG_LINE for line in block) and not block[-1].endswith((".", ","))


def join_heading(lines: list[str]) -> str:
    """Join heading lines: a wrapped title continues with a space, a new title gets a full stop."""
    out = lines[0]
    for line in lines[1:]:
        wrapped = line[:1].islower() or out.rstrip(" .").rsplit(" ", 1)[-1].lower() in COMMON
        out = out.rstrip(" .") + (" " if wrapped else ". ") + line
    return out


def tidy(text: str) -> str:
    text = re.sub(r"(\d)\s*[\"”']\s+(century|centuries)", r"\1th \2", text)  # 13" century
    text = re.sub(r"(\d)\s+(st|nd|rd|th)\b", r"\1\2", text)  # 19 th
    text = re.sub(r"\b([A-Z][a-z]+) \|(?=[\s,.;])", r"\1 I", text)  # roman numeral: "Solomon |"
    text = text.replace("|", " ")
    text = re.sub(r"\.{2}(?!\.)", ".", text)  # "war.." but keep ellipses
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def build_paragraphs(pages: dict[int, str]) -> list[tuple[int, str, bool]]:
    """Return (page, text, is_heading) for the book, merging text split across blocks and pages."""
    merged: list[tuple[int, list[str], bool]] = []
    for num, text in pages.items():
        for block in page_blocks(text):
            heading = is_heading(block)
            prev = merged[-1] if merged else None
            if prev and not prev[2] and not heading and not prev[1][-1].endswith(SENTENCE_END) and \
                    (block[0][:1].islower() or len(prev[1][-1]) >= LONG_LINE):
                prev[1].extend(block)  # sentence continues from the previous block/page
                continue
            if not heading and block[0][:1].islower():
                # a sentence interrupted by picture captions (which look like headings): drop them
                captions = 0
                while captions < len(merged) and captions < 3 and merged[-1 - captions][2]:
                    captions += 1
                if 0 < captions < len(merged) and not merged[-1 - captions][1][-1].endswith(SENTENCE_END):
                    del merged[-captions:]
                    merged[-1][1].extend(block)
                    continue
            merged.append((num, list(block), heading))
    out = []
    for num, lines, heading in merged:
        text = join_heading(lines) if heading else fix_drop_cap(join_lines(lines))
        out.append((num, tidy(text), heading))
    return out


def run_text(work: Path, first_page: int, last_page: int | None, part_words: int) -> None:
    """Clean the OCR'd pages and write them as parts of roughly part_words words, split at headings."""
    load_words()
    pages = {int(p.stem): p.read_text(encoding="utf8") for p in sorted((work / "pages").glob("*.txt"))}
    pages = {n: t for n, t in pages.items() if n >= first_page and (last_page is None or n <= last_page)}
    if not pages:
        sys.exit(f"no OCR'd pages in {work / 'pages'}; run the ocr stage first")
    HEADERS[:] = find_headers(pages)
    print(f"Running headers: {HEADERS}")

    # start a new part at a heading once part_words is reached, or anywhere once it is far exceeded
    parts: list[list[tuple[int, str, bool]]] = [[]]
    words = 0
    for par in build_paragraphs(pages):
        if words >= part_words and (par[2] or words >= part_words * 1.6):
            parts.append([])
            words = 0
        parts[-1].append(par)
        words += len(par[1].split())

    text_dir = work / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    for old in text_dir.glob("*.txt"):
        old.unlink()
    index = []
    last_heading = "Introduction"
    for i, part in enumerate(parts, 1):
        title = part[0][1] if part[0][2] else ("Introduction" if i == 1 else f"{last_heading} (continued)")
        last_heading = next((t for _, t, h in reversed(part) if h), last_heading)
        body = "\n\n".join(t for _, t, _ in part)
        (text_dir / f"{i:02d}.txt").write_text(f"{title}\n\n{body}\n", encoding="utf8")
        index.append(f"{i:02d}  pages {part[0][0]}-{part[-1][0]}  {len(body.split()):>6} words  {title}")
    (work / "book.txt").write_text(
        "\n\n".join(t for part in parts for _, t, _ in part) + "\n", encoding="utf8")
    print("\n".join(index))
    print(f"Text: {len(parts)} parts in {text_dir}, full text in {work / 'book.txt'}")


# ---------------------------------------------------------------- text to speech

def chunk_text(text: str, limit: int = 2500) -> list[str]:
    """Split into chunks of whole paragraphs (or sentences, for huge paragraphs) under limit chars."""
    pieces = []
    for par in text.split("\n\n"):
        while len(par) > limit:
            cut = par.rfind(". ", 0, limit) + 1 or limit
            pieces.append(par[:cut])
            par = par[cut:].lstrip()
        pieces.append(par)
    chunks, cur = [], ""
    for piece in pieces:
        if cur and len(cur) + len(piece) + 2 > limit:
            chunks.append(cur)
            cur = piece
        else:
            cur = f"{cur}\n\n{piece}" if cur else piece
    return chunks + [cur] if cur else chunks


async def synth(text: str, voice: str, out: Path, sem: asyncio.Semaphore) -> None:
    import edge_tts

    async with sem:
        for attempt in range(1, 6):
            try:
                tmp = out.with_suffix(".part")
                await edge_tts.Communicate(text, voice).save(str(tmp))
                tmp.rename(out)
                return
            except Exception as e:  # network errors, throttling, empty responses
                if attempt == 5:
                    raise RuntimeError(f"edge-tts failed for {out}: {e}") from e
                await asyncio.sleep(5 * attempt)


async def tts_part(txt: Path, voice: str, work: Path, album: str, sem: asyncio.Semaphore, total: int) -> Path:
    num = int(txt.stem)
    title, _, body = txt.read_text(encoding="utf8").partition("\n\n")
    seg_dir = work / "tts" / txt.stem
    seg_dir.mkdir(parents=True, exist_ok=True)
    chunks = chunk_text(body)
    # name segments by content so edited text or a new voice is re-synthesized, unchanged text is not
    segs = [seg_dir / f"{hashlib.sha1(f'{voice}\n{c}'.encode()).hexdigest()[:16]}.mp3" for c in chunks]
    await asyncio.gather(*(synth(c, voice, s, sem) for c, s in zip(chunks, segs) if not s.exists()))
    for stale in set(seg_dir.glob("*.mp3")) - set(segs):
        stale.unlink()

    safe = re.sub(r"[^\w\s,.()-]", "", title)[:80].strip()
    out = work / "audio" / f"{num:02d} - {safe}.mp3"
    out.parent.mkdir(exist_ok=True)
    listing = seg_dir / "list.txt"
    listing.write_text("".join(f"file '{s.name}'\n" for s in segs))
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
         "-metadata", f"title={num:02d} {title}", "-metadata", f"album={album}",
         "-metadata", f"track={num}/{total}", "-metadata", "genre=Audiobook", str(out)],
        check=True,
    )
    print(f"  done: {out.name}", flush=True)
    return out


async def run_tts(work: Path, voice: str, album: str, concurrency: int) -> None:
    parts = sorted((work / "text").glob("*.txt"))
    if not parts:
        sys.exit(f"no text parts in {work / 'text'}; run the text stage first")
    print(f"TTS: {len(parts)} parts with {voice}")
    sem = asyncio.Semaphore(concurrency)
    done = await asyncio.gather(*(tts_part(p, voice, work, album, sem, len(parts)) for p in parts))
    for stale in set((work / "audio").glob("*.mp3")) - set(done):
        stale.unlink()  # parts renamed or removed by a later text run
    print(f"Audio: {len(done)} files in {work / 'audio'}")


# ---------------------------------------------------------------- video (for YouTube)

def render_video(mp3: Path, title: str, num: int, total: int, album: str, cover: Path, font: str,
                 out: Path) -> None:
    """Still video: book cover on the left, part number and title on the right, the MP3 as audio."""
    import textwrap

    with tempfile.TemporaryDirectory() as tmp:
        head, body = Path(tmp) / "head.txt", Path(tmp) / "title.txt"
        head.write_text(f"{album}\nPart {num} of {total}", encoding="utf8")
        body.write_text(textwrap.fill(title, 30), encoding="utf8")
        graph = (
            "[0:v]scale=-2:1080,pad=1920:1080:0:0:color=0x0f3d2e,"
            f"drawtext=fontfile='{font}':textfile='{head}':fontcolor=0xd8d2c0:fontsize=40:"
            "line_spacing=12:x=760:y=150,"
            f"drawtext=fontfile='{font}':textfile='{body}':fontcolor=0xe8c27a:fontsize=60:"
            "line_spacing=20:x=760:y=(h-text_h)/2+40,format=yuv420p[v]"
        )
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", "1", "-i", str(cover), "-i", str(mp3),
             "-filter_complex", graph, "-map", "[v]", "-map", "1:a", "-c:v", "libopenh264", "-r", "1",
             "-c:a", "aac", "-b:a", "96k", "-shortest", "-movflags", "+faststart",
             "-metadata", f"title={title}", str(out.with_suffix(".part.mp4"))],
            check=True,
        )
    out.with_suffix(".part.mp4").rename(out)


def run_video(pdf: Path, work: Path, cover_page: int, album: str, jobs: int) -> None:
    """One MP4 per part plus its transcript, for YouTube's caption auto-sync."""
    parts = sorted((work / "text").glob("*.txt"))
    audio = {int(p.name[:2]): p for p in (work / "audio").glob("*.mp3")}
    if not parts or len(audio) < len(parts):
        sys.exit("run the text and tts stages first")
    video_dir = work / "video"
    video_dir.mkdir(exist_ok=True)

    cover = work / "cover.png"
    if not cover.exists():
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["pdfimages", "-png", "-f", str(cover_page), "-l", str(cover_page), str(pdf),
                            f"{tmp}/c"], check=True)
            images = sorted(Path(tmp).glob("c-*.png"))
            if not images:
                sys.exit(f"no image on page {cover_page} to use as the cover")
            shutil.copyfile(images[0], cover)  # tmp may be another filesystem: no rename
    font = subprocess.run(["fc-match", "-f", "%{file}", "sans-serif:bold"], capture_output=True, text=True,
                          check=True).stdout

    todo = []
    for txt in parts:
        num = int(txt.stem)
        title, _, body = txt.read_text(encoding="utf8").partition("\n\n")
        out = video_dir / audio[num].with_suffix(".mp4").name
        (video_dir / audio[num].with_suffix(".txt").name).write_text(body, encoding="utf8")  # transcript
        if not out.exists():
            todo.append((audio[num], title, num, len(parts), album, cover, font, out))
    print(f"Video: {len(parts)} parts, {len(todo)} to render")
    with ThreadPoolExecutor(jobs) as pool:
        futures = {pool.submit(render_video, *args): args[-1] for args in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            fut.result()
            print(f"  [{i}/{len(todo)}] {futures[fut].name}", flush=True)
    print(f"Videos and transcripts in {video_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--work", type=Path, help="work/output dir (default: output/<pdf name>)")
    ap.add_argument("--stage", choices=["ocr", "text", "tts", "video", "all"], default="all",
                    help="'all' runs ocr, text and tts; 'video' is separate")
    ap.add_argument("--lang", default="eng", help="tesseract language (eng, kat, ...)")
    ap.add_argument("--dpi", type=int, default=300, help="render resolution for pages that are not a single scan")
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    ap.add_argument("--first-page", type=int, default=1, help="skip front matter before this page")
    ap.add_argument("--last-page", type=int, help="skip back matter after this page")
    ap.add_argument("--part-words", type=int, default=4500, help="target words per MP3 (~150 per minute)")
    ap.add_argument("--voice", default="en-US-GuyNeural", help="see: edge-tts --list-voices")
    ap.add_argument("--tts-jobs", type=int, default=4, help="parallel edge-tts requests")
    ap.add_argument("--title", help="album tag (default: PDF name)")
    ap.add_argument("--cover-page", type=int, default=1, help="page whose image is the video background")
    args = ap.parse_args()

    pdf = args.pdf.expanduser().resolve()
    if not pdf.is_file():
        sys.exit(f"no such file: {pdf}")
    work = (args.work or DIR / "output" / pdf.stem).resolve()

    if args.stage in ("ocr", "all"):
        run_ocr(pdf, work, args.lang, args.dpi, args.jobs)
    if args.stage in ("text", "all"):
        run_text(work, args.first_page, args.last_page, args.part_words)
    album = args.title or pdf.stem.replace("_", " ")
    if args.stage in ("tts", "all"):
        asyncio.run(run_tts(work, args.voice, album, args.tts_jobs))
    if args.stage == "video":
        run_video(pdf, work, args.cover_page, album, max(1, args.jobs // 2))


if __name__ == "__main__":
    main()
