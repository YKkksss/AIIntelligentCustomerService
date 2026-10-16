import io
import zipfile

import pymupdf
import pytest

from aics.ingest.parsers import ParseError, UnsupportedFileTypeError, detect_file_type, parse_bytes
from aics.ingest.parsers import docx as docx_parser
from aics.ingest.parsers.markdown import parse_markdown_text
from aics.ingest.parsers.text import decode_text


def test_detect_file_type() -> None:
    assert detect_file_type("a.PDF", b"%PDF-1.7 ...") == "pdf"
    assert detect_file_type("a.markdown", "# 标题".encode()) == "md"
    assert detect_file_type("a.htm", b"<html></html>") == "html"
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type("a.exe", b"MZ")
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type("noext", b"x")
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type("fake.pdf", b"<html>")
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type("fake.docx", b"%PDF-1.4")
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type("bin.txt", b"\x00\x01\x02binary")


def test_decode_text_encodings() -> None:
    s = "退货运费由商家承担，签收后七天内可申请。" * 3
    assert decode_text(s.encode("gbk")) == s
    assert decode_text(b"\xef\xbb\xbf" + s.encode()) == s
    assert decode_text(s.encode("utf-16")) == s


def test_empty_and_unknown() -> None:
    with pytest.raises(ParseError):
        parse_bytes(b"   \n", "txt")
    with pytest.raises(UnsupportedFileTypeError):
        parse_bytes(b"x", "xls")
    with pytest.raises(ParseError):
        parse_bytes(b"<html><body></body></html>", "html")


def test_markdown_details() -> None:
    doc = parse_markdown_text(
        "标题一\n===\n\n正文 [链接](http://x) ![图](a.png)\n\n子标题\n---\n\n1. 第一\n2. 第二\n   续行\n"
    )
    assert [(b.type, b.level) for b in doc.blocks] == [
        ("heading", 1),
        ("paragraph", None),
        ("heading", 2),
        ("list", None),
    ]
    assert doc.blocks[1].text == "正文 链接 图"
    assert "续行" in doc.blocks[3].text


def _pdf(make) -> bytes:  # type: ignore[no-untyped-def]
    d = pymupdf.open()
    make(d)
    return d.tobytes()


def test_pdf_errors() -> None:
    with pytest.raises(ParseError, match="损坏"):
        parse_bytes(b"%PDF-1.4 garbage", "pdf")
    scanned = _pdf(lambda d: d.new_page().draw_rect(pymupdf.Rect(10, 10, 100, 100)))
    with pytest.raises(ParseError, match="扫描件"):
        parse_bytes(scanned, "pdf")
    d = pymupdf.open()
    d.new_page().insert_text((50, 50), "secret")
    enc = d.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    with pytest.raises(ParseError, match="加密"):
        parse_bytes(enc, "pdf")


def test_pdf_cross_page_paragraph_and_page_numbers() -> None:
    def make(d):  # type: ignore[no-untyped-def]
        p1 = d.new_page()
        p1.insert_text((60, 780), "This sentence continues on the", fontsize=11)
        p1.insert_text((290, 830), "1", fontsize=8)
        p2 = d.new_page()
        p2.insert_text((60, 80), "next page without break.", fontsize=11)
        p2.insert_text((290, 830), "2", fontsize=8)

    doc = parse_bytes(_pdf(make), "pdf")
    assert [b.text for b in doc.blocks] == [
        "This sentence continues on the next page without break."
    ]
    assert doc.blocks[0].page == 1


def test_docx_zip_bomb_and_corrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"0" * 4096)
    monkeypatch.setattr(docx_parser, "MAX_UNZIPPED_BYTES", 1024)
    with pytest.raises(ParseError, match="200MB"):
        parse_bytes(buf.getvalue(), "docx")
    monkeypatch.undo()
    with pytest.raises(ParseError, match="无法打开"):
        parse_bytes(buf.getvalue(), "docx")
    with pytest.raises(ParseError, match="损坏"):
        parse_bytes(b"PK\x03\x04broken", "docx")
