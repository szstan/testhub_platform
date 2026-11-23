import threading
from types import SimpleNamespace

import pytest

pytest.importorskip('airtest.core.api', reason='Airtest is required by OCRHelper')
pytest.importorskip('cv2')
pytest.importorskip('PIL')

from PIL import Image

from apps.app_automation.utils import ocr_helper as ocr_module
from apps.app_automation.utils.ocr_helper import OCRHelper


@pytest.fixture(autouse=True)
def isolate_reader_cache(monkeypatch):
    monkeypatch.setattr(OCRHelper, '_easyocr_readers', {})
    monkeypatch.setattr(OCRHelper, '_reader_lock', threading.Lock())


def fake_result(text):
    return [([[0, 0], [1, 0], [1, 1], [0, 1]], text, 0.99)]


def install_fake_reader(monkeypatch, reader):
    monkeypatch.setattr(ocr_module, 'EASYOCR_AVAILABLE', True)
    monkeypatch.setattr(
        ocr_module,
        'easyocr',
        SimpleNamespace(Reader=lambda *_args, **_kwargs: reader),
        raising=False,
    )


def test_unavailable_easyocr_raises_explicit_runtime_error(monkeypatch):
    monkeypatch.setattr(ocr_module, 'EASYOCR_AVAILABLE', False)

    with pytest.raises(RuntimeError, match='OCR is unavailable'):
        OCRHelper().recognize_text(Image.new('RGB', (10, 10)), use_cache=False)


def test_missing_local_model_raises_sanitized_runtime_error(monkeypatch, caplog):
    def missing_model_reader(*_args, **_kwargs):
        raise FileNotFoundError('private-model-path/model.pth')

    monkeypatch.setattr(ocr_module, 'EASYOCR_AVAILABLE', True)
    monkeypatch.setattr(
        ocr_module, 'easyocr', SimpleNamespace(Reader=missing_model_reader), raising=False
    )

    with pytest.raises(RuntimeError, match='EasyOCR initialization failed') as error:
        OCRHelper().recognize_text(Image.new('RGB', (10, 10)), use_cache=False)

    assert 'private-model-path' not in str(error.value)
    assert 'private-model-path' not in caplog.text
    assert error.value.__cause__ is None


def test_recognition_exception_raises_sanitized_runtime_error(monkeypatch, caplog):
    class FailingReader:
        def readtext(self, _image):
            raise RuntimeError('private recognition backend detail')

    install_fake_reader(monkeypatch, FailingReader())

    with pytest.raises(RuntimeError, match='OCR recognition failed') as error:
        OCRHelper().recognize_text(Image.new('RGB', (10, 10)), use_cache=False)

    assert 'private recognition backend detail' not in str(error.value)
    assert 'private recognition backend detail' not in caplog.text
    assert error.value.__cause__ is None


def test_successful_recognition_with_no_text_returns_empty_string(monkeypatch):
    class EmptyReader:
        def readtext(self, _image):
            return []

    install_fake_reader(monkeypatch, EmptyReader())

    assert OCRHelper().recognize_text(Image.new('RGB', (10, 10)), use_cache=False) == ''


def test_no_recognized_digits_raises_value_error(monkeypatch):
    class EmptyReader:
        def readtext(self, _image):
            return []

    install_fake_reader(monkeypatch, EmptyReader())

    with pytest.raises(ValueError, match='No digits recognized by OCR'):
        OCRHelper().recognize_number(Image.new('RGB', (10, 10)), use_cache=False)


def test_alphabetic_ocr_is_not_normalized_into_digits(monkeypatch):
    class AlphabeticReader:
        def readtext(self, _image):
            return fake_result('foo')

    install_fake_reader(monkeypatch, AlphabeticReader())

    with pytest.raises(ValueError, match='No digits recognized by OCR'):
        OCRHelper().recognize_number(Image.new('RGB', (10, 10)), use_cache=False)


def test_mixed_digit_ocr_corrects_ambiguous_o(monkeypatch):
    class MixedReader:
        def readtext(self, _image):
            return fake_result('1O')

    install_fake_reader(monkeypatch, MixedReader())

    assert OCRHelper().recognize_number(Image.new('RGB', (10, 10)), use_cache=False) == 10


def test_literal_zero_is_a_valid_recognized_number(monkeypatch):
    class ZeroReader:
        def readtext(self, _image):
            return fake_result('0')

    install_fake_reader(monkeypatch, ZeroReader())

    assert OCRHelper().recognize_number(Image.new('RGB', (10, 10)), use_cache=False) == 0
