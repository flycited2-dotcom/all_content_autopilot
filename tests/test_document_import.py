"""Exercise real parsers and content-loss boundaries, without model/network calls."""
import base64
import io
import json
import zlib
import zipfile

import pytest

from orchestrator_bot import document_import as parser


def zipped(entries):
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return result.getvalue()


@pytest.mark.parametrize('encoding', ['utf-8-sig', 'utf-16', 'cp1251'])
def test_plain_text_preserves_sku_price_zero_and_encoding(encoding):
    raw = 'Генератор CARVER PPG-5000D; 45455; остаток 0'
    result = parser.extract(raw.encode(encoding), 'C:\\unsafe\\price.TXT')
    assert result['text'] == raw
    assert result['metadata']['filename'] == 'price.TXT'
    assert result['metadata']['bytes'] == len(raw.encode(encoding))
    assert result['images'] == []


@pytest.mark.parametrize('extension,raw', [
    ('.csv', 'Модель;Цена;Остаток\n"CARVER PPG-5000D";45455;0\n"CARVER PPG-5000DE";52200;63'),
    ('.tsv', 'Модель\tЦена\tОстаток\nCARVER PPG-5000D\t45455\t0\nCARVER PPG-5000DE\t52200\t63')])
def test_delimited_tables_preserve_row_boundaries_and_all_columns(extension, raw):
    result = parser.extract(raw.encode(), 'price' + extension)
    assert result['metadata']['table_rows'] == 3
    assert 'Строка 2: ["CARVER PPG-5000D", "45455", "0"]' in result['text']
    assert 'Строка 3: ["CARVER PPG-5000DE", "52200", "63"]' in result['text']


@pytest.mark.parametrize('extension,raw', [
    ('.yaml', 'products:\n  - model: PPG-5000D\n    price: 45455.00\n    stock: 0'),
    ('.json', '{"products":[{"model":"PPG-5000D","price":45455.00,"stock":0}]}')])
def test_structured_data_keeps_decimal_spelling(extension, raw):
    assert parser.extract(raw.encode(), 'price' + extension)['text'] == raw


@pytest.mark.parametrize('raw', [
    '!!python/object/apply:os.system [echo should-not-run]',
    'a: &a [1, 2]\nb: *a',
    'price: 1\nprice: 999',
])
def test_yaml_executable_tags_aliases_and_duplicate_keys_are_rejected(raw):
    with pytest.raises(ValueError):
        parser.extract(raw.encode(), 'price.yaml')


def test_json_duplicate_keys_do_not_silently_discard_data():
    with pytest.raises(ValueError, match='повторяется ключ'):
        parser.extract(b'{"stock":0,"stock":100}', 'price.json')


def test_xlsx_reads_every_sheet_and_marks_missing_formula_results():
    import openpyxl
    workbook = openpyxl.Workbook()
    first = workbook.active
    first.title = 'CARVER'
    first.append(['Модель', 'Цена', 'Остаток'])
    first.append(['PPG-5000D', 45455, 0])
    second = workbook.create_sheet('LIFAN')
    second.sheet_state = 'hidden'
    second.append(['6GF-3', '=100*1.15', 2])
    data = io.BytesIO()
    workbook.save(data)
    result = parser.extract(data.getvalue(), 'price.xlsx')
    assert 'Лист: CARVER' in result['text']
    assert 'Лист: LIFAN (скрытый)' in result['text']
    assert '["PPG-5000D", 45455, 0]' in result['text']
    assert 'формула без сохранённого результата: значение неизвестно' in result['text']
    assert result['metadata']['warnings']
    assert result['metadata']['table_rows'] == 3


# A real small BIFF8/OLE XLS created once with xlwt, compressed for the fixture.
XLS = 'eJztWE1oE0EU/mYz+Wlp0rSmQquEUDBqrJCgBVGatbG1F60xhygiaGpzkJZUghe9WK09CoonxUuhCF6qXvxBBb2IB6GiB0EQEnv0JFTw0HZ983ZTW02hObQqzLfMmzdv5ps3u29mdmffTTeVJh62lfEbknBhwaqDZ4lNUKqrFIKgestSaiX3UbI0/ivU+SiQHjee+d96VQxVvMsw8EC+Igl8oXQS59A/UshH1hEpHkNOqDF0kRS4Q5YAWnlUzSzPsNzA8j63fM5yP1uuseyitiVxAtNmf2yPM4uPG+1cF4Dq9zFzPrElgRa8UbP40nVht3Wju3g2N/xvVoRlAyZBcevLF/LF3HAJIQrgJL5bEWC2slJfRrR9fe0CZP+x3O6tYr9pSGAU1mme4OM0IWddqsaDA92ZbG9mDkco1ODkgdgsk3K37JT75CEpIaKkdsm49EKEZbdMkZqiFnsl7dHpdN/Ozng83uO39USCCpGebKaeurLXfHDZmvfzWmggOYhG1pt4RQRpV5+79+394YG0eYoto7zP22+DLeoGYOGyYhA5wDWuRRljxg6WV7jXTay3sQzRjVEeTbc4ysExbnOVa6PkZxfjg7l1ib6N9PGvR5+Ex2fM7aRP9ZUvhqY+mhNop7fTIPHVNYYO0SFu31J4alZy4ewcn1m2/rGL+IygM3bLjpFoxDzqWW1iaZfU0xGLJcN5VootqrAFs9XzeEH+BbOTrxUrayi24hlVeAbzpMOzY3L3hodkjL0mjWY8Uo1ox/uFemhoaGhoaGhoaGisBPVtan97gr807aMG1KGS/+vMU1r42z8pNNYMGYzQdZ4Opr0oUF7EhZrmz0a4RaUvsUpO5X+hwjHyXsQQBngcQzX5VqBzkVh6P6smBmt2tRJq9r9QyzjX2P9PXnbMQg=='


def test_legacy_xls_reads_real_binary_workbook():
    result = parser.extract(zlib.decompress(base64.b64decode(XLS)), 'price.xls')
    assert 'Лист: CARVER' in result['text']
    assert 'PPG-5000D' in result['text']
    assert '45455.0, 149.0' in result['text']
    assert 'PPG-11000 DVR' in result['text']
    assert result['metadata']['table_rows'] == 3


def test_docx_preserves_body_table_and_footer_order():
    raw = zipped({'word/document.xml': '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
      <w:p><w:r><w:t>Прайс CARVER</w:t></w:r></w:p><w:tbl><w:tr>
      <w:tc><w:p><w:r><w:t>PPG-5000D</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>45455</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>149</w:t></w:r></w:p></w:tc>
      </w:tr></w:tbl><w:p><w:r><w:t>Наценка 15%</w:t></w:r></w:p></w:body></w:document>''',
      'word/footer1.xml': '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Цены без доставки</w:t></w:r></w:p></w:ftr>'})
    text = parser.extract(raw, 'price.docx')['text']
    assert text.index('Прайс CARVER') < text.index('PPG-5000D') < text.index('Наценка 15%')
    assert 'Строка 1: ["PPG-5000D", "45455", "149"]' in text
    assert 'Цены без доставки' in text


def test_docx_embedded_price_screenshot_is_returned_for_recognition():
    from PIL import Image
    image = io.BytesIO()
    Image.new('RGB', (200, 200), 'white').save(image, format='PNG')
    raw = zipped({'word/document.xml': '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <w:body><w:p><w:r><w:t>Прайс во вложенной картинке</w:t></w:r><w:r><w:drawing><a:blip r:embed="rId7"/></w:drawing></w:r></w:p></w:body></w:document>''',
      'word/_rels/document.xml.rels': '<Relationships><Relationship Id="rId7" Target="media/price.png"/></Relationships>',
      'word/media/price.png': image.getvalue()})
    result = parser.extract(raw, 'price.docx')
    assert 'Прайс во вложенной картинке' in result['text']
    assert len(result['images']) == 1
    assert result['metadata']['image_count'] == 1


def test_ods_repeated_rows_and_cells_are_preserved():
    raw = zipped({'content.xml': '''<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
     xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
     <office:body><office:spreadsheet><table:table table:name="CARVER"><table:table-row table:number-rows-repeated="2">
     <table:table-cell><text:p>PPG-5000D</text:p></table:table-cell><table:table-cell office:value="45455"/>
     <table:table-cell office:value="0"/></table:table-row></table:table></office:spreadsheet></office:body></office:document-content>'''})
    result = parser.extract(raw, 'price.ods')
    assert result['metadata']['table_rows'] == 2
    assert result['text'].count('["PPG-5000D", "45455", "0"]') == 2


def test_pdf_keeps_text_pages_and_renders_scanned_pages():
    import pymupdf
    from PIL import Image
    image = io.BytesIO()
    Image.new('RGB', (600, 800), 'white').save(image, format='PNG')
    doc = pymupdf.open()
    doc.new_page().insert_text((30, 40), 'CARVER PPG-5000D 45455 stock 149')
    page = doc.new_page()
    page.insert_image(page.rect, stream=image.getvalue())
    result = parser.extract(doc.tobytes(), 'price.pdf')
    doc.close()
    assert 'Страница PDF 1:' in result['text']
    assert 'CARVER PPG-5000D 45455 stock 149' in result['text']
    assert 'Страница PDF 2: изображение attachment_index=0' in result['text']
    assert len(result['images']) == 1
    from listener.attachments import decode_images
    assert len(decode_images({'message': {'attachments': result['images']}})) == 1


def test_mixed_pdf_header_does_not_hide_image_price_table():
    import pymupdf
    from PIL import Image
    image = io.BytesIO()
    Image.new('RGB', (600, 600), 'white').save(image, format='PNG')
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((30, 40), 'Supplier price list')
    page.insert_image(pymupdf.Rect(30, 80, 500, 700), stream=image.getvalue())
    result = parser.extract(doc.tobytes(), 'price.pdf')
    doc.close()
    assert 'Supplier price list' in result['text']
    assert len(result['images']) == 1


def test_pdf_scan_page_overflow_fails_without_partial_result(monkeypatch):
    import pymupdf
    monkeypatch.setattr(parser, 'MAX_IMAGES', 1)
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    with pytest.raises(ValueError, match='Разделите файл'):
        parser.extract(doc.tobytes(), 'price.pdf')
    doc.close()


def test_encrypted_pdf_explains_required_unencrypted_copy():
    import pymupdf
    doc = pymupdf.open()
    doc.new_page().insert_text((30, 40), 'CARVER')
    raw = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='owner-secret', user_pw='secret')
    doc.close()
    with pytest.raises(ValueError, match='PDF защищён паролем'):
        parser.extract(raw, 'price.pdf')


def test_rtf_text_and_unicode_escape():
    pytest.importorskip('striprtf')
    result = parser.extract(b'{\\rtf1\\ansi CARVER PPG-5000D 45455\\par Stock 149}', 'price.rtf')
    assert 'CARVER PPG-5000D 45455' in result['text']
    assert 'Stock 149' in result['text']


def test_old_doc_missing_converter_explains_conversion(monkeypatch):
    monkeypatch.setattr(parser.shutil, 'which', lambda _: None)
    with pytest.raises(ValueError, match='сохраните файл как .docx или PDF'):
        parser.extract(b'legacy document', 'price.doc')


@pytest.mark.parametrize('name,data', [('price.exe', b'bad'), ('price.pdf', b'bad'), ('price.xlsx', b'bad'),
                                      ('price.docx', b'bad'), ('price.txt', b''), ('price.txt', b'\x00\x01')])
def test_corrupt_empty_or_unsupported_documents_fail(name, data):
    with pytest.raises(ValueError):
        parser.extract(data, name)


def test_rejects_xml_entity_expansion():
    raw = zipped({'word/document.xml': '<!DOCTYPE x [<!ENTITY e "test">]><x>&e;</x>'})
    with pytest.raises(ValueError):
        parser.extract(raw, 'price.docx')


def test_oversized_table_is_not_silently_truncated(monkeypatch):
    monkeypatch.setattr(parser, 'MAX_ROWS', 1)
    with pytest.raises(ValueError, match='Разделите файл'):
        parser.extract(b'model;price\nCARVER;45455', 'price.csv')


def test_oversized_text_is_not_silently_truncated(monkeypatch):
    monkeypatch.setattr(parser, 'MAX_TEXT_CHARS', 10)
    with pytest.raises(ValueError, match='Разделите его'):
        parser.extract(b'12345678901', 'price.txt')
