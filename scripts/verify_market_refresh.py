"""Repeat the real HTTP refresh/evaluation chain; no mocked collector or automatic success claim.

This modifies the database served by --base-url. Use a dedicated test database for acceptance.
An HTTP pass is not proof of the latest trading-day data, full research coverage or returns.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def probe(base_url: str, attempts: int, timeout: float, interval: float) -> dict:
    reports = []
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        record = {'attempt': attempt, 'attempted_at': datetime.now(timezone.utc).isoformat(),
                  'http_status': None, 'chain_complete': False}
        request = Request(base_url.rstrip('/') + '/api/sectors/refresh', data=b'{}', method='POST',
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=timeout) as response:
                record['http_status'] = response.status
                payload = json.load(response)
            refresh, evaluation = payload.get('refresh', {}), payload.get('evaluation', {})
            record.update(refresh_status=refresh.get('status'), message=refresh.get('message'),
                          ranking_as_of=refresh.get('ranking_as_of'), requested_count=refresh.get('requested_count'),
                          price_updated=refresh.get('price_updated'), membership_updated=refresh.get('membership_updated'),
                          manual_evidence_refreshed=refresh.get('manual_evidence_refreshed'),
                          comparison_as_of=evaluation.get('comparison_as_of'),
                          generated_at=evaluation.get('generated_at'), coverage=evaluation.get('coverage'),
                          api_contract=payload.get('api_contract'))
            record['chain_complete'] = (record['http_status'] == 200 and refresh.get('target') == 'sectors'
                                         and refresh.get('status') == 'success'
                                         and payload.get('api_contract') == 'market-workbench-v2'
                                         and isinstance(evaluation.get('items'), list) and bool(evaluation['items'])
                                         and bool(evaluation.get('generated_at')))
        except HTTPError as exc:
            record['http_status'] = exc.code
            try:
                record['error'] = json.load(exc).get('error', {})
            except (ValueError, AttributeError):
                record['error'] = {'message': '服务返回非 JSON 错误'}
        except (URLError, OSError, ValueError, TypeError, AttributeError) as exc:
            record['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        record['elapsed_seconds'] = round(time.monotonic() - started, 3)
        reports.append(record)
        if attempt < attempts:
            time.sleep(interval)
    return {'transport': 'real_http', 'collector_mocked': False, 'attempts': reports,
            'repeated_chain_passed': all(record['chain_complete'] for record in reports),
            'latest_trading_day_verified': False, 'investment_effectiveness_verified': False,
            'limits': '连续接口成功不等于行情已到最近完整交易日，也不代表经营、估值、基金适用性或投资收益已通过验证。'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--attempts', type=int, default=5)
    parser.add_argument('--timeout', type=float, default=620)
    parser.add_argument('--interval', type=float, default=2)
    args = parser.parse_args()
    parsed = urlsplit(args.base_url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        parser.error('--base-url 必须是无凭据的 HTTP(S) 服务地址')
    if not 3 <= args.attempts <= 20 or not 0 < args.timeout <= 900 or not 1 <= args.interval <= 60:
        parser.error('attempts 为3–20，timeout 为(0,900]秒，interval 为1–60秒')
    result = probe(args.base_url, args.attempts, args.timeout, args.interval)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result['repeated_chain_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
