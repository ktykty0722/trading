import logging
import time

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)


def request_json(method: str, url: str, *, headers=None, params=None, json=None, timeout=None, max_retries=None):
    """
    requests 호출 공통 래퍼.
    - timeout(connect, read) 강제
    - 네트워크/5xx/429에 대해 재시도
    """
    timeout = timeout or (settings.HTTP_TIMEOUT_CONNECT, settings.HTTP_TIMEOUT_READ)
    max_retries = settings.HTTP_MAX_RETRIES if max_retries is None else max_retries

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            response = requests.request(
                method=method.upper(),
                url=url,
                headers=headers,
                params=params,
                json=json,
                timeout=timeout,
            )

            if response.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                sleep_s = 0.8 * (attempt + 1)
                logger.warning(
                    f"HTTP 재시도 대상 응답({response.status_code}) {url} "
                    f"[{attempt + 1}/{max_retries + 1}] - {sleep_s:.1f}s 대기"
                )
                time.sleep(sleep_s)
                continue

            response.raise_for_status()
            return response.json()
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError, ValueError) as e:
            last_error = e
            if attempt >= max_retries:
                break
            sleep_s = 0.8 * (attempt + 1)
            logger.warning(
                f"HTTP 호출 실패 재시도 {url} [{attempt + 1}/{max_retries + 1}]: {e} "
                f"- {sleep_s:.1f}s 대기"
            )
            time.sleep(sleep_s)

    raise last_error
