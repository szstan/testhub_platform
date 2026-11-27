import pytest

pytest.importorskip('airtest.core.api', reason='Airtest is required by OCRHelper')
pytest.importorskip('cv2')
pytest.importorskip('PIL')

from apps.app_automation.utils.ocr_helper import OCRHelper


@pytest.fixture
def number_helper(monkeypatch):
    def fake_recognize_text(self, text, use_cache=True, *, deadline=None):
        return text

    monkeypatch.setattr(OCRHelper, 'recognize_text', fake_recognize_text)
    return OCRHelper()


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('hello 0', 0),
        ('amount -1,234 units', -1234),
        ('value +42', 42),
        ('1O', 10),
    ],
)
def test_recognizes_one_number_token_surrounded_by_text(number_helper, text, expected):
    assert number_helper.recognize_number(text, use_cache=False) == expected


@pytest.mark.parametrize('text', ['hello', 'O', '1 of 2', 'hello 0 world 1'])
def test_rejects_missing_or_ambiguous_number_with_generic_error(number_helper, text):
    with pytest.raises(ValueError) as error:
        number_helper.recognize_number(text, use_cache=False)

    assert str(error.value) == 'No digits recognized by OCR or number is ambiguous'


def test_error_does_not_expose_raw_ocr_text(number_helper, caplog):
    text = 'PrivateBalanceMarker 1 of 2'

    with pytest.raises(ValueError) as error:
        number_helper.recognize_number(text, use_cache=False)

    assert 'PrivateBalanceMarker' not in str(error.value)
    assert 'PrivateBalanceMarker' not in caplog.text


def test_comma_separators_can_be_disabled(number_helper):
    with pytest.raises(ValueError):
        number_helper.recognize_number('1,234', allow_comma=False, use_cache=False)
