from contextlib import contextmanager
import importlib.util
import io
from pathlib import Path
from unittest.mock import Mock

import pytest

from certidoes import receita_android as android
from certidoes.errors import CertificateError
from certidoes.sources import receita


def pdf(document):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(300, 200)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    content = DecodedStreamObject()
    content.set_data(f'BT /F1 12 Tf 10 100 Td ({document}) Tj ET'.encode())
    page[NameObject('/Contents')] = writer._add_object(content)
    writer.add_metadata({'/Subject': 'synthetic fixture ' + 'x' * 1100})
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def test_default_dispatch_uses_redroid_once_without_legacy_fallback(monkeypatch):
    monkeypatch.delenv('RECEITA_EXECUTOR', raising=False)
    generate = Mock(return_value=object())
    legacy = Mock()
    monkeypatch.setattr(android, 'generate', generate)
    monkeypatch.setattr(receita, '_browser_generate', legacy)
    request = receita.ReceitaRequest('52998224725', '1990-01-01')
    assert receita.generate(request) is generate.return_value
    generate.assert_called_once_with(request)
    legacy.assert_not_called()


def test_unknown_executor_is_rejected(monkeypatch):
    monkeypatch.setenv('RECEITA_EXECUTOR', 'typo')
    with pytest.raises(CertificateError) as error:
        receita.generate(receita.ReceitaRequest('52998224725', '1990-01-01'))
    assert error.value.code == 'executor_invalido'


def test_successful_pdf_without_network_event_is_still_returned(monkeypatch, tmp_path):
    from certidoes import receita_android_flow

    @contextmanager
    def device(request, report):
        report['cleanup_ok'] = True
        yield 9229

    def run(request, **kwargs):
        path = request.output_dir / 'certificate.pdf'
        path.write_bytes(pdf(request.documento))
        return path

    monkeypatch.setattr(android, 'device', device)
    monkeypatch.setattr(receita_android_flow, 'run', run)
    result = android.generate(receita.ReceitaRequest('52998224725', '1990-01-01', output_dir=tmp_path))
    assert result.pdf_path.exists()
    assert result.metadata['executor'] == 'redroid'
    assert result.metadata['status'] == 'Emitida'
    assert result.metadata['captcha_solver_used'] is False
    diagnostic = (tmp_path / 'receita-android.json').read_text()
    assert '52998224725' not in diagnostic and '1990-01-01' not in diagnostic


def test_pdf_for_another_document_is_rejected(tmp_path):
    path = tmp_path / 'certificate.pdf'
    path.write_bytes(pdf('12345678909'))
    with pytest.raises(CertificateError) as error:
        android.validate_pdf(path, '52998224725')
    assert error.value.code == 'documento_divergente'


def test_busy_device_never_clears_another_requests_profile(monkeypatch, tmp_path):
    fcntl = pytest.importorskip('fcntl')
    lock_path = tmp_path / 'android.lock'
    monkeypatch.setenv('RECEITA_ANDROID_LOCK', str(lock_path))
    prepare, adb = Mock(), Mock()
    monkeypatch.setattr(android, 'prepare', prepare)
    monkeypatch.setattr(android, '_adb', adb)
    request = receita.ReceitaRequest('52998224725', '1990-01-01', request_timeout_s=.01)
    with lock_path.open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(CertificateError) as error:
            with android.device(request, {}):
                pytest.fail('Busy device lease must not be acquired')
    assert error.value.code == 'android_ocupado'
    prepare.assert_not_called()
    adb.assert_not_called()


def test_prod_transport_ignores_the_unsupported_chrome_extra_info_event():
    from collections import Counter, defaultdict
    from certidoes.browser.direct_cdp import DirectChrome
    import json

    client = DirectChrome.__new__(DirectChrome)
    client.sequence = 0
    client.commands, client.ignored_events = Counter(), Counter()
    client.handlers = defaultdict(list)
    client.socket = Mock()
    client.socket.recv.side_effect = [json.dumps({'method': 'Network.requestWillBeSentExtraInfo', 'params': {}}),
                                      json.dumps({'id': 1, 'result': {'ok': True}})]
    assert client.call('Page.enable') == {'ok': True}
    assert client.ignored_events['Network.requestWillBeSentExtraInfo'] == 1


def test_sdk_parser_rejects_a_non_sdk_disk():
    spec = importlib.util.spec_from_file_location('prepare_apks', Path(__file__).parents[1] / 'docker/receita/prepare_apks.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match='GPT'):
        module.product_extents(io.BytesIO(b'x' * 1024))
