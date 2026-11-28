from unittest.mock import MagicMock, Mock, patch

import pytest

from apps.core.http_transport import guarded_stream_get


@pytest.mark.parametrize('body_rejected', [False, True])
def test_guarded_stream_keeps_session_alive_and_closes_when_body_iteration_ends(body_rejected):
    response = Mock()
    session = MagicMock()
    session.request.return_value = response
    session.__enter__.return_value = session
    with patch('apps.core.http_transport.requests.Session', return_value=session):
        try:
            with guarded_stream_get('https://example.test/file', timeout=3) as streamed:
                assert streamed is response
                session.__exit__.assert_not_called()
                response.close.assert_not_called()
                assert session.trust_env is False
                kwargs = session.request.call_args.kwargs
                assert kwargs['stream'] is True
                assert kwargs['allow_redirects'] is False
                assert kwargs['timeout'] == 3
                assert session.mount.call_count == 2
                if body_rejected:
                    raise ValueError('byte budget exceeded')
        except ValueError:
            assert body_rejected
    response.close.assert_called_once()
    session.__exit__.assert_called_once()


def test_guarded_stream_refuses_proxy_override():
    with pytest.raises(ValueError, match='proxies'):
        with guarded_stream_get('https://example.test/file', proxies={'https': 'http://proxy.test'}):
            pytest.fail('proxy override accepted')
