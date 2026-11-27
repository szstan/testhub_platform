import threading
from types import SimpleNamespace

import pytest

pytest.importorskip('airtest.core.api', reason='Airtest is required by OCRHelper')
pytest.importorskip('cv2')
pytest.importorskip('PIL')

from PIL import Image

from apps.app_automation.utils import ocr_helper as ocr_module
from apps.app_automation.utils.ocr_helper import OCRHelper


def test_easyocr_readers_are_cached_by_language_tuple_and_gpu(monkeypatch):
    calls = []

    def reader_factory(languages, **kwargs):
        calls.append((languages, kwargs))
        return object()

    monkeypatch.setattr(ocr_module, 'EASYOCR_AVAILABLE', True)
    monkeypatch.setattr(ocr_module, 'easyocr', SimpleNamespace(Reader=reader_factory), raising=False)
    monkeypatch.setattr(OCRHelper, '_easyocr_readers', {})
    monkeypatch.setattr(OCRHelper, '_reader_lock', threading.Lock())

    english = OCRHelper.get_easyocr_reader(['en'], use_gpu=False)
    assert OCRHelper.get_easyocr_reader(['en'], use_gpu=False) is english
    chinese_english = OCRHelper.get_easyocr_reader(['ch_sim', 'en'], use_gpu=False)
    gpu_english = OCRHelper.get_easyocr_reader(['en'], use_gpu=True)

    assert len(calls) == 3
    assert calls[0][0] == ['en']
    assert calls[0][1]['gpu'] is False
    assert calls[0][1]['download_enabled'] is False
    assert calls[1][0] == ['ch_sim', 'en']
    assert calls[1][1]['download_enabled'] is False
    assert calls[2][1]['gpu'] is True
    assert calls[2][1]['download_enabled'] is False
    assert chinese_english is not english
    assert gpu_english is not english


def test_configured_model_directory_is_used_as_a_cache_boundary_without_downloads(monkeypatch, tmp_path):
    calls = []

    def reader_factory(languages, **kwargs):
        calls.append((languages, kwargs))
        return object()

    monkeypatch.setattr(ocr_module, 'EASYOCR_AVAILABLE', True)
    monkeypatch.setattr(ocr_module, 'easyocr', SimpleNamespace(Reader=reader_factory), raising=False)
    monkeypatch.setattr(OCRHelper, '_easyocr_readers', {})
    monkeypatch.setattr(OCRHelper, '_reader_lock', threading.Lock())

    from django.test import override_settings

    with override_settings(APP_OCR_MODEL_DIR=str(tmp_path / 'models-a')):
        first = OCRHelper.get_easyocr_reader(['en'], use_gpu=False)
        assert OCRHelper.get_easyocr_reader(['en'], use_gpu=False) is first
    with override_settings(APP_OCR_MODEL_DIR=str(tmp_path / 'models-b')):
        second = OCRHelper.get_easyocr_reader(['en'], use_gpu=False)

    assert first is not second
    assert len(calls) == 2
    assert calls[0][1] == {
        'gpu': False, 'download_enabled': False,
        'model_storage_directory': str(tmp_path / 'models-a'),
        'user_network_directory': str(tmp_path / 'models-a' / 'user_network'),
    }
    assert calls[1][1]['model_storage_directory'] == str(tmp_path / 'models-b')
    assert calls[1][1]['user_network_directory'] == str(tmp_path / 'models-b' / 'user_network')
    assert calls[1][1]['download_enabled'] is False


def test_ocr_result_cache_does_not_cross_language_configurations(monkeypatch, caplog):
    OCRHelper._ocr_cache.clear()
    reader_outputs = {'en': 'English-only', 'ch_sim': '中文结果'}

    class FakeReader:
        def __init__(self, result):
            self.result = result

        def readtext(self, _image):
            return [([ [0, 0], [1, 0], [1, 1], [0, 1] ], self.result, 0.99)]

    def get_reader(cls, languages=None, use_gpu=False):
        return FakeReader(reader_outputs[languages[0]])

    monkeypatch.setattr(OCRHelper, '_easyocr_readers', {})
    monkeypatch.setattr(OCRHelper, 'get_easyocr_reader', classmethod(get_reader))
    monkeypatch.setattr(ocr_module, 'EASYOCR_AVAILABLE', True)
    image = Image.new('RGB', (10, 10), color='white')
    caplog.set_level('INFO', logger=ocr_module.__name__)

    english = OCRHelper(['en'], use_gpu=False).recognize_text(image)
    chinese = OCRHelper(['ch_sim'], use_gpu=False).recognize_text(image)

    assert english == 'English-only'
    assert chinese == '中文结果'
    assert 'English-only' not in caplog.text
    assert '中文结果' not in caplog.text
