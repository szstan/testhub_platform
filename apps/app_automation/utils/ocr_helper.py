# -*- coding: utf-8 -*-
"""
OCR 工具类 - 基于 EasyOCR
"""
import os
import time
import logging
import hashlib
import re
import math
import threading
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageEnhance

try:
    import easyocr
    EASYOCR_AVAILABLE = True
except ImportError:
    EASYOCR_AVAILABLE = False

from airtest.core.api import G
from .. import device_actions
from ..bounded_inference import BoundedInference, InferenceBusy, InferenceTimeout

logger = logging.getLogger(__name__)

_NUMBER_TOKEN_PATTERN = re.compile(
    r'(?<![\w,+-])(?P<sign>[+-]?)(?P<digits>[0-9OoIl?]+(?:,[0-9OoIl?]+)*)(?![\w,+-])'
)
_NUMBER_TOKEN_NO_COMMAS_PATTERN = re.compile(
    r'(?<![\w,+-])(?P<sign>[+-]?)(?P<digits>[0-9OoIl?]+)(?![\w,+-])'
)
_OCR_DIGIT_TRANSLATION = str.maketrans({'o': '0', 'O': '0', 'l': '1', 'I': '1', '?': '1'})
_NUMBER_RECOGNITION_ERROR = 'No digits recognized by OCR or number is ambiguous'
_OCR_INFERENCE_MAX_SECONDS = 30.0
_OCR_INFERENCE = BoundedInference('app-ocr-inference')


def _run_ocr_inference(inference, deadline):
    return _OCR_INFERENCE.run(inference, deadline)


class OCRHelper:
    """OCR 辅助类 - 提供图像文字识别功能"""
    
    # OCR结果缓存：key为(坐标区域hash, 图片hash)，value为(识别结果, 时间戳)
    _ocr_cache = {}
    _cache_ttl = 2.0  # 缓存有效期2秒
    _cache_max_size = 50  # 最大缓存条目数
    
    # EasyOCR readers are worker-side OCR models, not device compatibility claims.
    _easyocr_readers = {}
    _reader_lock = threading.Lock()
    
    def __init__(self, languages=None, use_gpu=False):
        """
        初始化 OCR 助手
        
        Args:
            languages: OCR 识别语言列表，默认为 ['en']（英文）
                      可选：['ch_sim', 'en'] (简体中文和英文)
            use_gpu: 是否使用 GPU 加速，默认 False
        """
        if not EASYOCR_AVAILABLE:
            logger.warning("EasyOCR 未安装，OCR 功能将不可用。请运行: pip install easyocr")
        
        self.languages = self._normalize_languages(languages)
        self.use_gpu = bool(use_gpu)

    @staticmethod
    def _normalize_languages(languages):
        if languages is None:
            return ('en',)
        if isinstance(languages, str):
            languages = [part.strip() for part in languages.replace(',', '/').split('/') if part.strip()]
        if not isinstance(languages, (list, tuple)) or not languages:
            raise ValueError('OCR languages must be a non-empty sequence')
        normalized = tuple(str(language).strip() for language in languages if str(language).strip())
        if not normalized:
            raise ValueError('OCR languages must be a non-empty sequence')
        return normalized

    @staticmethod
    def _model_storage_directory():
        try:
            from django.conf import settings as django_settings
            if django_settings.configured:
                configured = getattr(django_settings, 'APP_OCR_MODEL_DIR', '')
                if configured:
                    return str(configured).strip() or None
                return os.path.join(str(django_settings.MEDIA_ROOT), 'app-automation', 'ocr-models')
        except Exception:
            pass
        return os.environ.get('APP_OCR_MODEL_DIR', '').strip() or None

    @classmethod
    def get_easyocr_reader(cls, languages=None, use_gpu=False):
        """
        获取或创建EasyOCR reader实例（延迟初始化，单例模式）
        
        Args:
            languages: 识别语言列表
            use_gpu: 是否使用 GPU
            
        Returns:
            easyocr.Reader 实例
        """
        if not EASYOCR_AVAILABLE:
            raise ImportError("EasyOCR 未安装，请运行: pip install easyocr")
        
        language_key = cls._normalize_languages(languages)
        gpu_key = bool(use_gpu)
        model_directory = cls._model_storage_directory()
        cache_key = (language_key, gpu_key, model_directory)
        with cls._reader_lock:
            reader = cls._easyocr_readers.get(cache_key)
            if reader is None:
                try:
                    logger.info('Initializing EasyOCR reader (languages=%s, GPU=%s)', language_key, gpu_key)
                    reader_options = {'gpu': gpu_key, 'download_enabled': False}
                    if model_directory:
                        reader_options['model_storage_directory'] = model_directory
                        reader_options['user_network_directory'] = os.path.join(model_directory, 'user_network')
                    reader = easyocr.Reader(list(language_key), **reader_options)
                    cls._easyocr_readers[cache_key] = reader
                    logger.info('EasyOCR reader initialized')
                except Exception:
                    logger.error('EasyOCR initialization failed')
                    raise RuntimeError(
                        'EasyOCR initialization failed; install the required model files locally'
                    ) from None
        return reader
    
    @staticmethod
    def _get_image_hash(img) -> str:
        """
        计算图片的hash值用于缓存
        
        Args:
            img: PIL Image 或 numpy array
            
        Returns:
            图片的 MD5 hash 值
        """
        if isinstance(img, Image.Image):
            img_array = np.array(img)
        else:
            img_array = img
        return hashlib.md5(img_array.tobytes()).hexdigest()
    
    @classmethod
    def _get_cache_key(cls, region: Tuple, img_hash: str) -> Tuple:
        """生成缓存key"""
        return (tuple(region), img_hash)
    
    @classmethod
    def _clean_cache(cls):
        """清理过期缓存"""
        current_time = time.time()
        expired_keys = [
            key for key, (_, timestamp) in cls._ocr_cache.items()
            if current_time - timestamp > cls._cache_ttl
        ]
        for key in expired_keys:
            cls._ocr_cache.pop(key, None)
        
        # 如果缓存仍然太大，删除最旧的条目
        if len(cls._ocr_cache) > cls._cache_max_size:
            sorted_items = sorted(
                cls._ocr_cache.items(),
                key=lambda x: x[1][1]  # 按时间戳排序
            )
            # 删除最旧的一半
            for key, _ in sorted_items[:len(sorted_items)//2]:
                cls._ocr_cache.pop(key, None)
    
    def recognize_text(self, img, min_confidence=0.3, use_cache=True, *, deadline=None) -> str:
        """Recognize text from an in-memory image before the monotonic deadline."""
        if not EASYOCR_AVAILABLE:
            logger.error('OCR is unavailable; EasyOCR and local model files are required')
            raise RuntimeError('OCR is unavailable; EasyOCR and local model files are required')

        now = time.monotonic()
        deadline = now + _OCR_INFERENCE_MAX_SECONDS if deadline is None else float(deadline)
        deadline = min(deadline, now + _OCR_INFERENCE_MAX_SECONDS)
        if not math.isfinite(deadline) or deadline <= now:
            raise RuntimeError('OCR recognition exceeded its deadline')

        def infer():
            cache_key = None
            if use_cache:
                cache_key = (
                    self.languages, self.use_gpu, self._model_storage_directory(),
                    self._get_image_hash(img), float(min_confidence),
                )
                cached = self._ocr_cache.get(cache_key)
                if cached and time.time() - cached[1] < self._cache_ttl:
                    return cached[0], cache_key

            if isinstance(img, Image.Image):
                image = img.convert('RGB') if img.mode != 'RGB' else img
                width, height = image.size
                if width < 1000 or height < 200:
                    image = image.resize((width * 2, height * 2), Image.LANCZOS)
                img_array = np.array(image)
            else:
                img_array = img
                if len(img_array.shape) == 2:
                    img_array = np.stack([img_array] * 3, axis=-1)
            if time.monotonic() >= deadline:
                raise InferenceTimeout('OCR recognition exceeded its deadline')
            try:
                reader = self.get_easyocr_reader(self.languages, self.use_gpu)
            except Exception:
                raise RuntimeError(
                    'EasyOCR initialization failed; install the required model files locally'
                ) from None
            try:
                results = reader.readtext(img_array)
                text_items = [
                    (bbox[0][0], text) for bbox, text, confidence in results
                    if float(confidence) >= min_confidence
                ]
                text_items.sort(key=lambda item: item[0])
                return ' '.join(item[1] for item in text_items).strip(), cache_key
            except Exception:
                raise RuntimeError('OCR recognition failed') from None

        try:
            combined_text, cache_key = _run_ocr_inference(infer, deadline)
        except InferenceTimeout:
            raise RuntimeError('OCR recognition exceeded its deadline') from None
        except InferenceBusy:
            raise RuntimeError('OCR recognition is unavailable while a previous request is still running') from None
        except RuntimeError as error:
            if str(error).startswith('EasyOCR initialization failed') or str(error) == 'OCR recognition failed':
                raise
            logger.error('OCR recognition failed')
            raise RuntimeError('OCR recognition failed') from None
        except Exception:
            logger.error('OCR recognition failed')
            raise RuntimeError('OCR recognition failed') from None

        if time.monotonic() >= deadline:
            raise RuntimeError('OCR recognition exceeded its deadline')
        if use_cache and cache_key:
            self._ocr_cache[cache_key] = (combined_text, time.time())
            self._clean_cache()
        logger.info('OCR recognition completed (%d characters)', len(combined_text))
        return combined_text

    def recognize_number(self, img, allow_comma=True, use_cache=True, *, deadline=None) -> int:
        """Recognize the single unambiguous number in an in-memory image."""
        text = self.recognize_text(img, use_cache=use_cache, deadline=deadline)
        pattern = _NUMBER_TOKEN_PATTERN if allow_comma else _NUMBER_TOKEN_NO_COMMAS_PATTERN
        numbers = []

        for match in pattern.finditer(text):
            digits = match.group('digits')
            if not any('0' <= character <= '9' for character in digits):
                continue

            normalized_digits = digits.translate(_OCR_DIGIT_TRANSLATION).replace(',', '')
            try:
                numbers.append(int(match.group('sign') + normalized_digits))
            except ValueError:
                raise ValueError(_NUMBER_RECOGNITION_ERROR) from None

        if len(numbers) != 1:
            raise ValueError(_NUMBER_RECOGNITION_ERROR)

        logger.info('Number extraction completed')
        return numbers[0]
    
    @staticmethod
    def crop_region(
        region: Tuple[int, int, int, int], screenshot_path: Optional[str] = None,
        *, deadline=None,
    ) -> Image.Image:
        """Crop an OCR region from a bounded ADB screenshot or local image."""
        now = time.monotonic()
        deadline = now + 2.0 if deadline is None else float(deadline)
        if not math.isfinite(deadline) or deadline <= now:
            raise RuntimeError('OCR screenshot exceeded its deadline')

        image_bytes = None
        local_path = screenshot_path if screenshot_path and os.path.exists(screenshot_path) else None
        if local_path is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('OCR screenshot exceeded its deadline')
            image_bytes = device_actions.capture_screenshot(
                G.DEVICE, timeout=min(remaining, 30.0),
            )

        def decode_and_crop():
            img_cv = (
                cv2.imread(local_path) if local_path else
                cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
            )
            if img_cv is None:
                raise RuntimeError('OCR screenshot returned invalid image data')
            x1, y1, x2, y2 = region
            height, width = img_cv.shape[:2]
            if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1 or x2 > width or y2 > height:
                raise ValueError('OCR region is outside the screenshot bounds')
            cropped = img_cv[y1:y2, x1:x2]
            pil_img = Image.fromarray(cv2.cvtColor(cropped, cv2.COLOR_BGR2RGB))
            return ImageEnhance.Contrast(pil_img).enhance(2.0)

        try:
            return _run_ocr_inference(decode_and_crop, deadline)
        except InferenceTimeout:
            raise RuntimeError('OCR screenshot exceeded its deadline') from None
        except InferenceBusy:
            raise RuntimeError('OCR recognition is unavailable while a previous request is still running') from None
        except ValueError:
            raise ValueError('OCR region is outside the screenshot bounds') from None
        except Exception:
            raise RuntimeError('OCR screenshot returned invalid image data') from None

    def recognize_region_text(
        self, region: Tuple[int, int, int, int], screenshot_path: Optional[str] = None,
        *, deadline=None,
    ) -> str:
        now = time.monotonic()
        deadline = now + 2.0 if deadline is None else float(deadline)
        img = self.crop_region(region, screenshot_path, deadline=deadline)
        return self.recognize_text(img, deadline=deadline)

    def recognize_region_number(
        self, region: Tuple[int, int, int, int], screenshot_path: Optional[str] = None,
        *, deadline=None,
    ) -> int:
        now = time.monotonic()
        deadline = now + 2.0 if deadline is None else float(deadline)
        img = self.crop_region(region, screenshot_path, deadline=deadline)
        return self.recognize_number(img, deadline=deadline)


_ocr_helper_instances = {}


def get_ocr_helper(languages=None, use_gpu=False) -> OCRHelper:
    """Return an OCR helper cached by language set and worker-side GPU option."""
    language_key = OCRHelper._normalize_languages(languages)
    cache_key = (language_key, bool(use_gpu))
    helper = _ocr_helper_instances.get(cache_key)
    if helper is None:
        helper = OCRHelper(languages=language_key, use_gpu=bool(use_gpu))
        _ocr_helper_instances[cache_key] = helper
    return helper
