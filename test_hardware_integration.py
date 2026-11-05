"""Opt-in ESP32-S3 serial test; uses an isolated temporary database."""
import os
import time

import pytest
from fastapi.testclient import TestClient

import app as module


def test_esp32_live_serial_to_api(tmp_path):
    port = os.environ.get('BOARDLAB_TEST_PORT')
    if not port:
        pytest.skip('Set BOARDLAB_TEST_PORT to run the real-board serial test')
    application = module.create_app(tmp_path / 'board.sqlite3', tmp_path / 'uploads')
    with TestClient(application, base_url='http://127.0.0.1') as client:
        project_id = client.get('/api/projects').json()[0]['id']
        result = client.post('/api/serial/start', json={
            'project_id': project_id, 'port': port, 'baud': 115200,
        })
        assert result.status_code == 200, result.text
        try:
            deadline = time.monotonic() + 12
            samples = []
            while time.monotonic() < deadline:
                samples = client.get(f'/api/projects/{project_id}/samples').json()
                if len(samples) >= 2:
                    break
                time.sleep(.2)
            assert len(samples) >= 2, 'No JSON samples received from the demo board'
            recent = [s['values'] for s in samples]
            assert all('count' in value and 'voltage' in value for value in recent)
            assert recent[-1]['count'] > recent[0]['count']
            assert all(s['source'] == 'serial' for s in samples)
        finally:
            client.post('/api/serial/stop')
