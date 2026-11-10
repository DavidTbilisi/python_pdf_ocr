# python_pdf_ocr

OCR for scanned PDF books, with a pipeline that turns them into MP3 audiobooks.

- `audiobook.py`: scanned PDF → cleaned text → MP3 audiobook (described below)
- `main.py`: older script that OCRs page images in `images/` to a text file (Georgian by default)

## audiobook.py

```sh
python3 audiobook.py ~/Downloads/History_Of_Georgia.pdf --first-page 4 --last-page 508
```

This runs three stages. Each stage caches its results under `output/<pdf name>/`, so if a run is interrupted or a stage is re-run, finished work is skipped:

| Stage  | What it does | Output |
|--------|--------------|--------|
| `ocr`  | Extracts each page's scanned image and OCRs it with tesseract, using all CPU cores | `pages/0001.txt` … |
| `text` | Removes running headers, page numbers, footnotes, picture captions and scan noise. Repairs the decorated first letter of sections, rejoins hyphenated words and paragraphs split across pages, and splits the book into parts of about 30 minutes at section headings | `text/01.txt` …, `book.txt` (the whole cleaned text) |
| `tts`  | Reads each part aloud with [edge-tts](https://github.com/rany2/edge-tts) and joins the pieces into one tagged MP3 per part | `audio/01 - <section title>.mp3` … |

A 540-page book takes about 20 minutes to OCR on 8 cores and an hour or two of text-to-speech. The result is roughly 25 hours of audio.

### Requirements

System tools (Fedora package names; the Debian/Ubuntu names are similar):

```sh
sudo dnf install poppler-utils tesseract tesseract-langpack-eng ffmpeg-free hunspell-en-US
pip install --user edge-tts
```

- `poppler-utils`: `pdfinfo`, `pdfimages`, `pdftoppm`
- `tesseract` plus a language pack for the book's language (`tesseract-langpack-kat` for Georgian)
- `ffmpeg`: joins the speech segments into one MP3 per part
- `hunspell-en-US`: English dictionary used to tell real words from OCR noise

### Recommended workflow

Look over the text before generating hours of audio:

```sh
# 1. OCR every page
python3 audiobook.py BOOK.pdf --stage ocr

# 2. Find the first and last pages of the main text (skip the cover, title page, plates, index...)
#    by checking output/BOOK/pages/NNNN.txt, then build the text
python3 audiobook.py BOOK.pdf --stage text --first-page 4 --last-page 508

# 3. Read output/BOOK/book.txt and the list of parts printed by the text stage, then:
python3 audiobook.py BOOK.pdf --stage tts --title "History of Georgia"
```

If you change the text stage options and run it again, the `tts` stage regenerates only the parts of the audio whose text changed.

### Options

| Option | Default | |
|--------|---------|---|
| `--stage {ocr,text,tts,all}` | `all` | Run one stage, or all three in order |
| `--first-page N`, `--last-page N` | whole book | Pages to include in the text (front and back matter are usually excluded) |
| `--lang` | `eng` | Tesseract language |
| `--voice` | `en-US-GuyNeural` | Voice to read the book; list them with `edge-tts --list-voices` (e.g. `en-GB-RyanNeural`) |
| `--part-words` | `4500` | Target length of each MP3 in words (about 150 words per minute). A part ends at the next section heading after this many words, or anywhere once it reaches 1.6 times this |
| `--title` | PDF file name | Album tag written to the MP3s |
| `--jobs` | number of CPUs | Number of pages OCR'd in parallel |
| `--tts-jobs` | `4` | Number of speech requests sent in parallel |
| `--dpi` | `300` | Resolution used only for pages that don't consist of a single scanned image |
| `--work DIR` | `output/<pdf name>` | Directory for cached and final output |

### Things to know

- **edge-tts is an online service.** It sends the book's text to Microsoft's speech service. For fully offline speech you would need to replace the `tts` stage, e.g. with Piper.
- **The `text` stage only works for English books for now.** It keeps a block of text only if most of its words are in the English dictionary, so it would throw away almost all of a Georgian book. The `ocr` stage (`--lang kat`) and the voices (`--voice ka-GE-GiorgiNeural`) do support Georgian, but the cleanup would first need a Georgian word list.
- **OCR isn't perfect.** About 3% of words in a typical scanned book come out wrong, and some paragraph openings stay garbled. Spot-check `book.txt` before generating audio.
- **Running headers are detected automatically** (short lines that appear at the top of at least 10% of pages). Footnotes are recognized by a `*` or a superscript mark at the bottom of a page.
