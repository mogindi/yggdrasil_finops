import os
import tempfile
import unittest
import zlib
from pathlib import Path

from document_service import DocumentError, DocumentService


class DocumentServiceLogoFormatTests(unittest.TestCase):
    def setUp(self):
        os.environ["CLOUDKITTY_CURRENCY"] = "USD"

    def _write_png(self, path: Path, width: int, height: int, color_type: int, raw_scanlines: bytes):
        def chunk(kind: bytes, payload: bytes) -> bytes:
            import struct
            import zlib as _z

            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", _z.crc32(kind + payload) & 0xFFFFFFFF)

        import struct

        ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
        compressed = zlib.compress(raw_scanlines)
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
        path.write_bytes(png)

    def test_png_logo_is_embedded_in_pdf(self):
        service = DocumentService()
        with tempfile.TemporaryDirectory() as tmp:
            logo_path = Path(tmp) / "logo.png"
            # One RGB pixel: filter=0, R=255,G=0,B=0
            self._write_png(logo_path, width=1, height=1, color_type=2, raw_scanlines=b"\x00\xff\x00\x00")
            pdf = service.build_invoice_pdf({"invoice_id": "inv_1"}, logo_path=str(logo_path))

        self.assertIn(b"/Filter /FlateDecode", pdf)
        self.assertIn(b"/Subtype /Image", pdf)

    def test_png_with_alpha_is_rejected(self):
        service = DocumentService()
        with tempfile.TemporaryDirectory() as tmp:
            logo_path = Path(tmp) / "logo_alpha.png"
            # One RGBA pixel: filter=0, R,G,B,A
            self._write_png(logo_path, width=1, height=1, color_type=6, raw_scanlines=b"\x00\x00\x00\x00\xff")
            with self.assertRaises(DocumentError):
                service.build_invoice_pdf({"invoice_id": "inv_1"}, logo_path=str(logo_path))


if __name__ == "__main__":
    unittest.main()
