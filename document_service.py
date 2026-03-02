import base64
import html
from pathlib import Path


class DocumentError(Exception):
    pass


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _jpeg_size(data: bytes) -> tuple[int, int]:
    if not data.startswith(b"\xff\xd8"):
        raise DocumentError("Only JPEG logos are supported")
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        while i < len(data) and data[i] == 0xFF:
            i += 1
        if i >= len(data):
            break
        marker = data[i]
        i += 1
        if marker in (0xD8, 0xD9):
            continue
        if i + 2 > len(data):
            break
        seg_len = (data[i] << 8) + data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2) and i + 7 < len(data):
            height = (data[i + 3] << 8) + data[i + 4]
            width = (data[i + 5] << 8) + data[i + 6]
            return width, height
        i += seg_len
    raise DocumentError("Unable to parse JPEG logo")


class DocumentService:
    def _load_logo(self, logo_path: str | None) -> tuple[bytes | None, int, int]:
        if not logo_path:
            return None, 0, 0
        path = Path(logo_path)
        if not path.exists() or not path.is_file():
            raise DocumentError(f"Logo file not found: {logo_path}")
        data = path.read_bytes()
        width, height = _jpeg_size(data)
        return data, width, height

    def _build_pdf(self, title: str, lines: list[str], logo_path: str | None = None) -> bytes:
        logo_data, logo_width, logo_height = self._load_logo(logo_path)
        objects: list[bytes] = []

        y = 780
        content_lines: list[str] = ["BT /F1 18 Tf 40 800 Td (" + _escape_pdf_text(title) + ") Tj ET"]
        if logo_data:
            draw_w = 120
            draw_h = int(draw_w * (logo_height / logo_width))
            content_lines.append(f"q {draw_w} 0 0 {draw_h} 40 {y - draw_h + 10} cm /Im1 Do Q")
            y -= draw_h + 10
        for line in lines:
            safe = _escape_pdf_text(line)
            content_lines.append(f"BT /F1 11 Tf 40 {y} Td ({safe}) Tj ET")
            y -= 18
        content_stream = "\n".join(content_lines).encode("latin-1", errors="replace")

        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")  # 1
        if logo_data:
            objects.append(
                f"<< /Type /XObject /Subtype /Image /Width {logo_width} /Height {logo_height} /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(logo_data)} >>\nstream\n".encode("latin-1")
                + logo_data
                + b"\nendstream"
            )  # 2
            resources = b"<< /Font << /F1 1 0 R >> /XObject << /Im1 2 0 R >> >>"
            content_obj_num = 3
            page_obj_num = 4
            pages_obj_num = 5
            catalog_obj_num = 6
        else:
            resources = b"<< /Font << /F1 1 0 R >> >>"
            content_obj_num = 2
            page_obj_num = 3
            pages_obj_num = 4
            catalog_obj_num = 5

        objects.append(f"<< /Length {len(content_stream)} >>\nstream\n".encode("latin-1") + content_stream + b"\nendstream")
        objects.append(
            f"<< /Type /Page /Parent {pages_obj_num} 0 R /MediaBox [0 0 595 842] /Resources ".encode("latin-1")
            + resources
            + f" /Contents {content_obj_num} 0 R >>".encode("latin-1")
        )
        objects.append(f"<< /Type /Pages /Kids [{page_obj_num} 0 R] /Count 1 >>".encode("latin-1"))
        objects.append(f"<< /Type /Catalog /Pages {pages_obj_num} 0 R >>".encode("latin-1"))

        out = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for idx, obj in enumerate(objects, start=1):
            offsets.append(len(out))
            out.extend(f"{idx} 0 obj\n".encode("latin-1"))
            out.extend(obj)
            out.extend(b"\nendobj\n")

        xref_start = len(out)
        out.extend(f"xref\n0 {len(objects)+1}\n".encode("latin-1"))
        out.extend(b"0000000000 65535 f \n")
        for off in offsets[1:]:
            out.extend(f"{off:010d} 00000 n \n".encode("latin-1"))
        out.extend(f"trailer\n<< /Size {len(objects)+1} /Root {catalog_obj_num} 0 R >>\nstartxref\n{xref_start}\n%%EOF\n".encode("latin-1"))
        return bytes(out)

    def build_invoice_pdf(self, invoice: dict, logo_path: str | None = None) -> bytes:
        lines = [
            f"Invoice ID: {invoice.get('invoice_id', '')}",
            f"Project ID: {invoice.get('project_id', '')}",
            f"Customer: {invoice.get('customer', {}).get('name', '')}",
            f"Customer Email: {invoice.get('customer', {}).get('email', '')}",
            f"Description: {invoice.get('description', '')}",
            f"Amount Due: {invoice.get('amount_due', 0):.2f} {invoice.get('currency', 'USD')}",
            f"Amount Paid: {invoice.get('amount_paid', 0):.2f} {invoice.get('currency', 'USD')}",
            f"Status: {invoice.get('status', '')}",
            f"Created At: {invoice.get('created_at', '')}",
            f"Due At: {invoice.get('due_at', '')}",
        ]
        return self._build_pdf("Invoice", lines, logo_path=logo_path)

    def build_receipt_pdf(self, receipt: dict, invoice: dict, logo_path: str | None = None) -> bytes:
        lines = [
            f"Receipt ID: {receipt.get('receipt_id', '')}",
            f"Invoice ID: {receipt.get('invoice_id', '')}",
            f"Project ID: {receipt.get('project_id', '')}",
            f"Amount Paid: {receipt.get('amount_paid', 0):.2f} {receipt.get('currency', 'USD')}",
            f"Paid At: {receipt.get('paid_at', '')}",
            f"Payment Method: {receipt.get('payment_method', '')}",
            f"Payment Reference: {receipt.get('payment_reference', '')}",
            f"Invoice Status After Payment: {invoice.get('status', '')}",
            f"Created At: {receipt.get('created_at', '')}",
        ]
        return self._build_pdf("Receipt", lines, logo_path=logo_path)

    @staticmethod
    def build_pdf_html_page(title: str, filename: str, pdf_bytes: bytes) -> str:
        encoded = base64.b64encode(pdf_bytes).decode("ascii")
        data_url = f"data:application/pdf;base64,{encoded}"
        safe_title = html.escape(title)
        safe_filename = html.escape(filename)
        return f"""<!DOCTYPE html>
<html lang=\"en\"><head><meta charset=\"utf-8\" /><title>{safe_title}</title></head>
<body><a href=\"{data_url}\" download=\"{safe_filename}\">Download PDF</a>
<iframe src=\"{data_url}\" width=\"100%\" height=\"800\"></iframe></body></html>"""
