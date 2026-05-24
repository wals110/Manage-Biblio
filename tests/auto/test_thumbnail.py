"""Tests pour lib/thumbnail.py — génération de miniatures PDF/ePub.

Structure du cache : `.thumbnail-cache/{stem}/{1,2,...}.jpg`
"""

import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from lib.thumbnail import (
    THUMBNAIL_HEIGHT,
    THUMBNAIL_WIDTH,
    _generate_placeholder_thumbnail,
    clear_cache,
    compute_content_key,
    count_pages,
    generate_thumbnail,
    get_cache_stats,
    get_doc_dir,
    save_pil_images_as_thumbnails,
)


def _make_test_image(width: int = 800, height: int = 1200, color=(255, 0, 0)) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_minimal_epub(tmpdir: Path, with_cover: bool = True) -> Path:
    epub_path = tmpdir / "test.epub"
    with zipfile.ZipFile(epub_path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>""")
        if with_cover:
            opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata/>
  <manifest>
    <item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/>
  </manifest>
  <spine/>
</package>"""
            zf.writestr("content.opf", opf)
            zf.writestr("cover.png", _make_test_image())
        else:
            opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata/>
  <manifest/>
  <spine/>
</package>"""
            zf.writestr("content.opf", opf)
    return epub_path


def _make_epub2_with_meta_cover(tmpdir: Path) -> Path:
    epub_path = tmpdir / "test_epub2.epub"
    with zipfile.ZipFile(epub_path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>""")
        opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata>
    <meta name="cover" content="cover-id"/>
  </metadata>
  <manifest>
    <item id="cover-id" href="img/cover.png" media-type="image/png"/>
  </manifest>
  <spine/>
</package>"""
        zf.writestr("content.opf", opf)
        zf.writestr("img/cover.png", _make_test_image(color=(0, 255, 0)))
    return epub_path


class TestPlaceholderThumbnail(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_placeholder_creates_jpeg(self):
        out = self.tmp / "placeholder.jpg"
        result = _generate_placeholder_thumbnail("Test message", out)
        self.assertTrue(result)
        self.assertTrue(out.exists())
        with Image.open(out) as img:
            self.assertEqual(img.size, (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT))
            self.assertEqual(img.format, "JPEG")

    def test_placeholder_multiline(self):
        out = self.tmp / "multi.jpg"
        result = _generate_placeholder_thumbnail("Ligne 1\nLigne 2", out)
        self.assertTrue(result)
        self.assertTrue(out.exists())


class TestEpubThumbnail(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_epub3_with_cover(self):
        epub = _make_minimal_epub(self.tmp, with_cover=True)
        doc_dir = self.tmp / "cache" / "test"
        n = generate_thumbnail(epub, doc_dir, n_pages=1)
        self.assertEqual(n, 1)
        self.assertTrue((doc_dir / "1.jpg").exists())
        with Image.open(doc_dir / "1.jpg") as img:
            self.assertEqual(img.size, (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT))

    def test_epub2_with_meta_cover(self):
        epub = _make_epub2_with_meta_cover(self.tmp)
        doc_dir = self.tmp / "cache" / "test_epub2"
        n = generate_thumbnail(epub, doc_dir, n_pages=1)
        self.assertEqual(n, 1)
        self.assertTrue((doc_dir / "1.jpg").exists())

    def test_epub_without_cover_falls_back_to_placeholder(self):
        epub = _make_minimal_epub(self.tmp, with_cover=False)
        doc_dir = self.tmp / "cache" / "test"
        n = generate_thumbnail(epub, doc_dir, n_pages=1)
        self.assertEqual(n, 1)
        self.assertTrue((doc_dir / "1.jpg").exists())

    def test_epub_n_pages_4_only_generates_1(self):
        """ePub n'a qu'une cover, donc même avec n_pages=4 on ne génère que 1 page."""
        epub = _make_minimal_epub(self.tmp, with_cover=True)
        doc_dir = self.tmp / "cache" / "test"
        n = generate_thumbnail(epub, doc_dir, n_pages=4)
        self.assertEqual(n, 1)
        self.assertTrue((doc_dir / "1.jpg").exists())
        self.assertFalse((doc_dir / "2.jpg").exists())

    def test_epub_start_page_2_returns_0(self):
        """start_page > 1 sur ePub : pas de page interne, retourne 0."""
        epub = _make_minimal_epub(self.tmp, with_cover=True)
        doc_dir = self.tmp / "cache" / "test"
        n = generate_thumbnail(epub, doc_dir, n_pages=1, start_page=2)
        self.assertEqual(n, 0)

    def test_corrupt_epub(self):
        bad = self.tmp / "corrupt.epub"
        bad.write_bytes(b"not a zip file")
        doc_dir = self.tmp / "cache" / "corrupt"
        n = generate_thumbnail(bad, doc_dir, n_pages=1)
        self.assertEqual(n, 1)
        self.assertTrue((doc_dir / "1.jpg").exists())


class TestUnsupportedFormat(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_txt_file_returns_placeholder(self):
        txt = self.tmp / "test.txt"
        txt.write_text("hello")
        doc_dir = self.tmp / "cache" / "test"
        n = generate_thumbnail(txt, doc_dir, n_pages=1)
        self.assertEqual(n, 1)
        self.assertTrue((doc_dir / "1.jpg").exists())


class TestCountPages(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_count_pages_empty(self):
        self.assertEqual(count_pages(self.tmp, "missing"), 0)

    def test_count_pages_three(self):
        doc = self.tmp / "doc"
        doc.mkdir()
        (doc / "1.jpg").write_bytes(b"x")
        (doc / "2.jpg").write_bytes(b"x")
        (doc / "3.jpg").write_bytes(b"x")
        # Un fichier non-numérique doit être ignoré
        (doc / "thumb.jpg").write_bytes(b"x")
        self.assertEqual(count_pages(self.tmp, "doc"), 3)

    def test_get_doc_dir(self):
        d = get_doc_dir(self.tmp, "Algorithms")
        self.assertEqual(d, self.tmp / "Algorithms")


class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_clear_cache_removes_legacy_jpgs(self):
        """Format legacy : .jpg à plat."""
        for i in range(3):
            (self.tmp / f"thumb_{i}.jpg").write_bytes(b"x" * 100)
        (self.tmp / "other.txt").write_text("keep me")

        count, freed = clear_cache(self.tmp)
        self.assertEqual(count, 3)
        self.assertEqual(freed, 300)
        self.assertTrue((self.tmp / "other.txt").exists())

    def test_clear_cache_removes_subdirs(self):
        """Format nouveau : sous-dossiers."""
        for name in ["doc1", "doc2"]:
            d = self.tmp / name
            d.mkdir()
            (d / "1.jpg").write_bytes(b"x" * 50)
            (d / "2.jpg").write_bytes(b"x" * 50)
        count, freed = clear_cache(self.tmp)
        self.assertEqual(count, 4)
        self.assertEqual(freed, 200)
        self.assertFalse((self.tmp / "doc1").exists())
        self.assertFalse((self.tmp / "doc2").exists())

    def test_clear_cache_mixed_formats(self):
        """Mix legacy + nouveau."""
        (self.tmp / "old.jpg").write_bytes(b"x" * 100)
        d = self.tmp / "new_doc"
        d.mkdir()
        (d / "1.jpg").write_bytes(b"x" * 200)
        count, freed = clear_cache(self.tmp)
        self.assertEqual(count, 2)
        self.assertEqual(freed, 300)

    def test_clear_cache_empty_dir(self):
        count, freed = clear_cache(self.tmp)
        self.assertEqual(count, 0)
        self.assertEqual(freed, 0)

    def test_clear_cache_nonexistent_dir(self):
        count, freed = clear_cache(self.tmp / "missing")
        self.assertEqual(count, 0)
        self.assertEqual(freed, 0)

    def test_get_cache_stats_subdirs(self):
        d = self.tmp / "doc"
        d.mkdir()
        (d / "1.jpg").write_bytes(b"x" * 1024)
        (d / "2.jpg").write_bytes(b"x" * 2048)
        stats = get_cache_stats(self.tmp)
        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["size_bytes"], 3072)

    def test_get_cache_stats_legacy(self):
        (self.tmp / "a.jpg").write_bytes(b"x" * 1024)
        (self.tmp / "b.jpg").write_bytes(b"x" * 2048)
        stats = get_cache_stats(self.tmp)
        self.assertEqual(stats["count"], 2)
        self.assertEqual(stats["size_bytes"], 3072)

    def test_get_cache_stats_nonexistent(self):
        stats = get_cache_stats(self.tmp / "missing")
        self.assertEqual(stats["count"], 0)
        self.assertEqual(stats["size_bytes"], 0)


class TestComputeContentKey(unittest.TestCase):
    """compute_content_key returns a stable MD5 of the file's head bytes,
    independent of the file path."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="klodo-thumb-key-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_16_char_hex(self):
        f = self.tmpdir / "foo.pdf"
        f.write_bytes(b"%PDF-1.4 some content")
        key = compute_content_key(f)
        self.assertIsNotNone(key)
        self.assertEqual(len(key), 16)
        self.assertRegex(key, r"^[0-9a-f]{16}$")

    def test_same_content_same_key_regardless_of_path(self):
        # The whole point of this key: survives renames.
        a = self.tmpdir / "original-name.pdf"
        b = self.tmpdir / "renamed-totally-differently.pdf"
        payload = b"%PDF-1.4 same bytes here"
        a.write_bytes(payload)
        b.write_bytes(payload)
        self.assertEqual(compute_content_key(a), compute_content_key(b))

    def test_different_content_different_key(self):
        a = self.tmpdir / "a.pdf"
        b = self.tmpdir / "b.pdf"
        a.write_bytes(b"%PDF-1.4 first")
        b.write_bytes(b"%PDF-1.4 second")
        self.assertNotEqual(compute_content_key(a), compute_content_key(b))

    def test_missing_file_returns_none(self):
        self.assertIsNone(compute_content_key(self.tmpdir / "ghost.pdf"))

    def test_empty_file_returns_none(self):
        f = self.tmpdir / "empty.pdf"
        f.write_bytes(b"")
        self.assertIsNone(compute_content_key(f))


class TestSavePilImagesAsThumbnails(unittest.TestCase):
    """save_pil_images_as_thumbnails persists in-memory PIL images so the
    LLM pipeline can capitalize on its cover extraction."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="klodo-thumb-save-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _img(self, color=(200, 50, 50)):
        return Image.new("RGB", (800, 1200), color)

    def test_writes_n_jpgs(self):
        imgs = [self._img(), self._img(color=(50, 200, 50)),
                self._img(color=(50, 50, 200))]
        n = save_pil_images_as_thumbnails(imgs, self.tmpdir)
        self.assertEqual(n, 3)
        for i in (1, 2, 3):
            f = self.tmpdir / f"{i}.jpg"
            self.assertTrue(f.exists())
            # All at the standard thumbnail size
            with Image.open(f) as out:
                self.assertLessEqual(out.width, THUMBNAIL_WIDTH)
                self.assertLessEqual(out.height, THUMBNAIL_HEIGHT)

    def test_idempotent_skips_existing(self):
        imgs = [self._img()]
        save_pil_images_as_thumbnails(imgs, self.tmpdir)
        # Second call should skip (overwrite=False is the default)
        n = save_pil_images_as_thumbnails(imgs, self.tmpdir)
        self.assertEqual(n, 0)

    def test_overwrite_true_rewrites(self):
        imgs = [self._img()]
        save_pil_images_as_thumbnails(imgs, self.tmpdir)
        n = save_pil_images_as_thumbnails(imgs, self.tmpdir, overwrite=True)
        self.assertEqual(n, 1)

    def test_empty_list_no_op(self):
        n = save_pil_images_as_thumbnails([], self.tmpdir)
        self.assertEqual(n, 0)

    def test_creates_doc_dir_if_missing(self):
        nested = self.tmpdir / "deeply" / "nested" / "newdir"
        self.assertFalse(nested.exists())
        n = save_pil_images_as_thumbnails([self._img()], nested)
        self.assertEqual(n, 1)
        self.assertTrue(nested.exists())
        self.assertTrue((nested / "1.jpg").exists())


if __name__ == "__main__":
    unittest.main()
