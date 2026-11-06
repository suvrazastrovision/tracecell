from io import BytesIO
import json
import xml.etree.ElementTree as ET

from PIL import Image
import pytest

from pbmc_platform.app.plots import render_plot


POLICY = json.dumps({'species': 'mouse', 'min_genes': 200, 'max_genes': 10000, 'max_mito_pct': 20})
PAYLOAD = json.dumps({'total_counts': [0, 1, 200, 200000], 'detected_genes': [0, 1, 180, 12000],
                      'mitochondrial_pct': [None, 0, 10, 100], 'passing': [False, False, True, False]}).encode()


@pytest.mark.parametrize('kind', ['counts', 'genes', 'mitochondrial', 'scatter'])
@pytest.mark.parametrize('format', ['png', 'jpeg', 'pdf', 'svg'])
def test_plot_exports(kind, format):
    data = render_plot(PAYLOAD, POLICY, kind, format)
    if format in ('png', 'jpeg'):
        with Image.open(BytesIO(data)) as image:
            assert image.size == (2400, 1620)
            assert image.format == ('JPEG' if format == 'jpeg' else 'PNG')
            assert image.info['dpi'][0] == pytest.approx(300, abs=.1)
            assert image.convert('RGB').getpixel((0, 0)) == (255, 255, 255)
    elif format == 'pdf':
        assert data.startswith(b'%PDF-') and b'%%EOF' in data[-100:]
    else:
        root = ET.fromstring(data)
        assert root.tag.endswith('svg')
        assert '#0072b2' in data.decode().lower() and '#d55e00' in data.decode().lower()


@pytest.mark.parametrize('kind', ['counts', 'genes', 'mitochondrial', 'scatter'])
def test_log_scale_handles_zero_and_extreme_values(kind):
    data = render_plot(PAYLOAD, POLICY, kind, 'svg', 'log')
    assert b'nan' not in data.lower() and len(data) > 1000


@pytest.mark.parametrize('kind,format,scale', [('bad', 'png', 'linear'), ('counts', 'html', 'linear'),
                                            ('counts', 'png', 'bad')])
def test_plot_rejects_unsupported_options(kind, format, scale):
    with pytest.raises(ValueError):
        render_plot(PAYLOAD, POLICY, kind, format, scale)
