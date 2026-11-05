from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import csv
import io
import json
import re
import subprocess
import sys
import sqlite3
import time
import zipfile
import pytest
from fastapi.testclient import TestClient
import app as module


@pytest.fixture
def client(tmp_path):
    application=module.create_app(tmp_path/'test.sqlite3',tmp_path/'uploads')
    with TestClient(application,base_url='http://127.0.0.1') as c:yield c


def create_project(c,name='环境监测板'):
    return c.post('/api/projects',json={'name':name,'board':'STM32F103','description':'调试'}).json()['id']


def test_independent_workspace_created_once_and_preserves_existing_projects(tmp_path):
    db=tmp_path/'legacy.sqlite3'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE projects(id INTEGER PRIMARY KEY,name TEXT NOT NULL,board TEXT NOT NULL,description TEXT NOT NULL,created_at TEXT NOT NULL)')
        c.execute("INSERT INTO projects(name,board,description,created_at) VALUES ('已有项目','STM32','保留','2026-01-01')")
    for _ in range(2):
        with TestClient(module.create_app(db,tmp_path/'uploads'),base_url='http://127.0.0.1') as client:
            projects=client.get('/api/projects').json()
            assert len(projects)==2
            assert projects[0]['is_workspace']==1 and projects[0]['name']=='独立调试'
            assert any(x['name']=='已有项目' and x['board']=='STM32' and x['is_workspace']==0 for x in projects)
            workspace=projects[0]['id']
            assert client.post(f'/api/projects/{workspace}/samples',json={'values':{'v':3.3}}).status_code==201


def test_project_notes_isolation(client):
    a=create_project(client);b=create_project(client,'另一块板')
    r=client.post(f'/api/projects/{a}/notes',json={'category':'故障记录','title':'串口乱码','content':'检查波特率'});assert r.status_code==201
    assert len(client.get(f'/api/projects/{a}/notes').json())==1
    assert client.get(f'/api/projects/{b}/notes').json()==[]
    assert client.get('/api/projects/999/notes').status_code==404


def test_edit_project_preserves_associated_records(client):
    ident=create_project(client)
    workspace=next(x['id'] for x in client.get('/api/projects').json() if x['is_workspace'])
    client.post(f'/api/projects/{ident}/notes',json={'category':'调试日志','title':'启动','content':'保持记录'})
    result=client.put(f'/api/projects/{ident}',json={'name':'控制板','board':'MSPM0','description':'更新后'})
    assert result.status_code==200 and result.json()['board']=='MSPM0'
    assert client.get(f'/api/projects/{ident}/notes').json()[0]['content']=='保持记录'
    assert client.put(f'/api/projects/{workspace}',json={'name':'重命名'}).status_code==400
    assert client.put('/api/projects/999',json={'name':'不存在'}).status_code==404


def test_threshold_alert_recovery_and_project_isolation(client):
    a=create_project(client);b=create_project(client,'另一块板')
    created=client.post(f'/api/projects/{a}/rules',json={'channel':'temperature','minimum':0,'maximum':75});assert created.status_code==201
    rule_id=created.json()['id']
    assert client.put(f'/api/projects/{b}/rules/{rule_id}',json={'channel':'temperature','maximum':75}).status_code==404
    assert client.put(f'/api/projects/{a}/rules/{rule_id}',json={'channel':'temperature','minimum':0,'maximum':75}).status_code==200
    assert client.post(f'/api/projects/{a}/rules',json={'channel':'temperature','maximum':90}).status_code==409
    for n in [80,90,75,76]:assert client.post(f'/api/projects/{a}/samples',json={'values':{'temperature':n}}).status_code==201
    events=client.get(f'/api/projects/{a}/alerts').json()
    assert [x['status'] for x in events]==['active','recovered']
    assert client.get(f'/api/projects/{b}/alerts').json()==[]
    assert client.post(f'/api/projects/{a}/rules',json={'channel':'x','minimum':10,'maximum':3}).status_code==422


def test_real_serial_reader_fragmented_lines_and_invalid(monkeypatch,client):
    ident=create_project(client)
    class FakePort:
        def __init__(self):self.chunks=[b'{"temperature":',b'23.5,"ignored":"text"}',b'\ninvalid\n',b'{"temperature":80}\n'];self.closed=False
        def read(self,n):
            if self.chunks:return self.chunks.pop(0)
            time.sleep(.01);return b''
        def cancel_read(self):pass
        def close(self):self.closed=True
    fake=FakePort()
    monkeypatch.setattr(module.list_ports,'comports',lambda:[SimpleNamespace(device='COM99',description='Fake port')])
    monkeypatch.setattr(module.serial,'Serial',lambda *a,**kw:fake)
    assert client.post('/api/serial/start',json={'project_id':ident,'port':'COM99','baud':115200}).status_code==200
    assert client.post('/api/serial/start',json={'project_id':ident,'port':'COM99','baud':115200}).status_code==409
    deadline=time.monotonic()+3
    while client.get('/api/serial').json()['received']<2 and time.monotonic()<deadline:time.sleep(.02)
    state=client.post('/api/serial/stop').json()
    assert not state['connected'] and state['received']==2 and state['invalid']==1 and fake.closed
    logs=client.get(f'/api/projects/{ident}/serial/logs').json()
    assert [x['valid'] for x in logs]==[True,False,True] and logs[1]['text']=='invalid'
    other=create_project(client,'不相关项目')
    assert client.get(f'/api/projects/{other}/serial/logs').json()==[]
    assert [x['values']['temperature'] for x in client.get(f'/api/projects/{ident}/samples').json()]==[23.5,80]
    assert all(x['source']=='serial' for x in client.get(f'/api/projects/{ident}/samples').json())


def test_serial_port_must_be_enumerated(client,monkeypatch):
    ident=create_project(client);monkeypatch.setattr(module.list_ports,'comports',lambda:[])
    assert client.post('/api/serial/start',json={'project_id':ident,'port':'COM1','baud':115200}).status_code==400


def test_serial_recovers_after_oversized_and_invalid_utf8_lines(monkeypatch,client):
    ident=create_project(client)
    class FakePort:
        def __init__(self):
            self.chunks=[b'x'*(module.MAX_LINE+1),b'ignored\n\xff\n{"v":3.3}\n']
        def read(self,n):
            if self.chunks:return self.chunks.pop(0)
            time.sleep(.01);return b''
        def cancel_read(self):pass
        def close(self):pass
    monkeypatch.setattr(module.list_ports,'comports',lambda:[SimpleNamespace(device='COM99',description='Fake')])
    monkeypatch.setattr(module.serial,'Serial',lambda *a,**kw:FakePort())
    assert client.post('/api/serial/start',json={'project_id':ident,'port':'COM99','baud':115200}).status_code==200
    try:
        deadline=time.monotonic()+3
        while client.get('/api/serial').json()['received']<1 and time.monotonic()<deadline:time.sleep(.02)
        state=client.get('/api/serial').json()
        assert state['received']==1 and state['invalid']==2
        assert [x['values']['v'] for x in client.get(f'/api/projects/{ident}/samples').json()]==[3.3]
    finally:
        client.post('/api/serial/stop')


def test_serial_send_text_hex_and_workspace_protection(monkeypatch,client):
    ident=create_project(client);other=create_project(client,'其他项目')
    class FakePort:
        def __init__(self):self.writes=[]
        def read(self,n):time.sleep(.01);return b''
        def write(self,data):self.writes.append(data);return len(data)
        def cancel_read(self):pass
        def close(self):pass
    fake=FakePort()
    monkeypatch.setattr(module.list_ports,'comports',lambda:[SimpleNamespace(device='COM99',description='Fake')])
    monkeypatch.setattr(module.serial,'Serial',lambda *a,**kw:fake)
    assert client.post('/api/serial/send',json={'project_id':ident,'data':'abc'}).status_code==409
    assert client.post('/api/serial/start',json={'project_id':ident,'port':'COM99','baud':115200}).status_code==200
    assert client.post('/api/serial/send',json={'project_id':other,'data':'abc'}).status_code==409
    assert client.post('/api/serial/send',json={'project_id':ident,'format':'hex','data':'F'}).status_code==422
    assert client.post('/api/serial/send',json={'project_id':ident,'format':'hex','data':'AA','ending':'lf'}).status_code==422
    assert client.post('/api/serial/send',json={'project_id':ident,'data':'  hi  ','ending':'crlf'}).json()['sent_bytes']==8
    assert client.post('/api/serial/send',json={'project_id':ident,'format':'hex','data':'AA 00 ff'}).json()['sent_bytes']==3
    assert fake.writes==[b'  hi  \r\n',b'\xaa\x00\xff']
    logs=client.get(f'/api/projects/{ident}/serial/logs').json()
    assert [x['direction'] for x in logs]==['tx','tx']
    client.post('/api/serial/stop')


def test_serial_partial_send_reports_failure(monkeypatch,client):
    ident=create_project(client)
    class FakePort:
        def read(self,n):time.sleep(.01);return b''
        def write(self,data):return 1
        def cancel_read(self):pass
        def close(self):pass
    monkeypatch.setattr(module.list_ports,'comports',lambda:[SimpleNamespace(device='COM99',description='Fake')])
    monkeypatch.setattr(module.serial,'Serial',lambda *a,**kw:FakePort())
    assert client.post('/api/serial/start',json={'project_id':ident,'port':'COM99','baud':9600}).status_code==200
    response=client.post('/api/serial/send',json={'project_id':ident,'data':'abc'})
    assert response.status_code==503 and '1/3' in response.json()['detail']
    assert '部分发送' in client.get(f'/api/projects/{ident}/serial/logs').json()[0]['text']
    client.post('/api/serial/stop')


def test_hardware_debugger_mi_flow_and_project_isolation(client,tmp_path):
    ident=create_project(client);other=create_project(client,'其他板卡')
    gdb=tmp_path/'arm-none-eabi-gdb.exe';gdb.write_bytes(b'fake')
    elf=tmp_path/'firmware.axf';elf.write_bytes(b'\x7fELF'+bytes([1,1,1])+bytes(11)+(40).to_bytes(2,'little'))
    source=tmp_path/'main.c';source.write_text('\n'.join(f'// line {i}' for i in range(1,31)),encoding='utf8')
    class FakeProcess:
        stdin=stdout=stderr=None
        def __init__(self):self.alive=True
        def poll(self):return None if self.alive else 0
        def terminate(self):self.alive=False
        def wait(self,timeout):return 0
        def kill(self):self.alive=False
    class FakeIO:
        def __init__(self):self.commands=[]
        def write(self,cmd,**kwargs):
            match=re.match(r'(\d+)(.*)',cmd);token=int(match[1]);command=match[2];self.commands.append(command)
            extra=[];payload={};message='done'
            if command.startswith('-target-select'):
                message='connected';extra=[{'type':'notify','message':'stopped','payload':{'reason':'signal-received','frame':{'func':'main','fullname':str(source),'line':'12'}}}]
            elif command.startswith('-break-insert'):payload={'bkpt':{'number':'1'}}
            elif command=='-break-list':payload={'BreakpointTable':{'body':[{'bkpt':{'number':'1','original-location':'main.c:12'}}]}}
            elif command.startswith('-data-evaluate-expression'):payload={'value':'42'}
            elif command=='-data-list-register-names':payload={'register-names':['r0','r1']}
            elif command=='-data-list-register-values x':payload={'register-values':[{'number':'0','value':'0x2a'}]}
            elif command=='-exec-continue':message='running'
            elif command=='-exec-interrupt':extra=[{'type':'notify','message':'stopped','payload':{'reason':'signal-received'}}]
            elif command=='-exec-next':message='running';extra=[{'type':'notify','message':'stopped','payload':{'reason':'end-stepping-range'}}]
            return [{'type':'result','token':token,'message':message,'payload':payload},*extra]
        def get_gdb_response(self,**kwargs):return []
    proc=FakeProcess();fake=FakeIO();client.app.state.debugger.factory=lambda _: (proc,fake)
    payload={'target':'stm32h723','gdb_path':str(gdb),'elf_path':str(elf),'port':61234}
    result=client.post(f'/api/projects/{ident}/debug/connect',json=payload)
    assert result.status_code==200 and result.json()['run_state']=='stopped'
    assert any(cmd.startswith('-file-exec-and-symbols') for cmd in fake.commands)
    assert '-target-select remote 127.0.0.1:61234' in fake.commands
    assert not any(cmd.startswith(('-target-download','-exec-run')) for cmd in fake.commands)
    assert client.post(f'/api/projects/{other}/debug/action',json={'action':'continue'}).status_code==409
    assert client.get(f'/api/projects/{other}/debug').json()['foreign'] is True
    viewed=client.get(f'/api/projects/{ident}/debug/source').json()
    assert viewed['line']==12 and viewed['lines'][11]['text']=='// line 12' and viewed['lines'][11]['current'] is True
    assert client.get(f'/api/projects/{other}/debug/source').status_code==409
    breakpoint=client.post(f'/api/projects/{ident}/debug/breakpoints',json={'location':'main.c:12'})
    assert breakpoint.status_code==200 and breakpoint.json()['bkpt']['number']=='1'
    assert client.get(f'/api/projects/{ident}/debug/breakpoints').json()[0]['number']=='1'
    assert client.delete(f'/api/projects/{ident}/debug/breakpoints/1').status_code==200
    assert client.delete(f'/api/projects/{other}/debug/breakpoints/1').status_code==409
    values=client.post(f'/api/projects/{ident}/debug/variables',json={'names':['counter','foo()']}).json()
    assert values[0]['value']=='42' and 'error' in values[1]
    assert client.get(f'/api/projects/{ident}/debug/registers').json()==[{'name':'r0','value':'0x2a'}]
    initial_seq=client.get(f'/api/projects/{ident}/debug').json()['stop_seq']
    stepped=client.post(f'/api/projects/{ident}/debug/action',json={'action':'step_over'}).json()
    assert stepped['run_state']=='stopped' and stepped['stop_seq']>initial_seq
    assert client.post(f'/api/projects/{ident}/debug/action',json={'action':'continue'}).json()['run_state']=='running'
    assert client.get(f'/api/projects/{ident}/debug/source').status_code==409
    assert client.post(f'/api/projects/{ident}/debug/action',json={'action':'pause'}).json()['run_state']=='stopped'
    assert client.post(f'/api/projects/{other}/debug/disconnect').status_code==409
    assert client.post(f'/api/projects/{ident}/debug/disconnect').status_code==200 and not proc.alive


def test_hardware_debugger_rejects_wrong_or_missing_files(client,tmp_path):
    ident=create_project(client);wrong=tmp_path/'gdb.exe';wrong.write_bytes(b'fake')
    elf=tmp_path/'firmware.elf';elf.write_bytes(b'not elf')
    payload={'target':'esp32s3','gdb_path':str(wrong),'elf_path':str(elf),'port':3333}
    assert client.post(f'/api/projects/{ident}/debug/connect',json=payload).status_code==409
    proper=tmp_path/'xtensa-esp32s3-elf-gdb.exe';proper.write_bytes(b'fake');payload['gdb_path']=str(proper)
    result=client.post(f'/api/projects/{ident}/debug/connect',json=payload)
    assert result.status_code==409 and 'ELF' in result.json()['detail']
    elf.write_bytes(b'\x7fELF'+bytes([1,1,1])+bytes(11)+(40).to_bytes(2,'little'))
    result=client.post(f'/api/projects/{ident}/debug/connect',json=payload)
    assert result.status_code==409 and '架构' in result.json()['detail']


def test_debug_tool_discovery_limits_target(client,monkeypatch):
    monkeypatch.setattr(module,'find_gdb_tools',lambda target: ['C:/tools/'+target+'/gdb.exe'])
    assert client.get('/api/debug/tools',params={'target':'esp32s3'}).json()['paths']==['C:/tools/esp32s3/gdb.exe']
    assert client.get('/api/debug/tools',params={'target':'stm32h723'}).status_code==200
    assert client.get('/api/debug/tools',params={'target':'something-else'}).status_code==422


def test_tool_status_separates_software_from_serial_evidence(client,monkeypatch):
    monkeypatch.setattr(module,'find_gdb_tools',lambda target:['C:/gdb.exe'] if target=='stm32h723' else [])
    monkeypatch.setattr(module,'find_related_tools',lambda target:{'openocd':[],'utility':['C:/programmer.exe']})
    monkeypatch.setattr(module,'find_development_tools',lambda target:{'compiler':['C:/gcc.exe'],'vscode':[]})
    monkeypatch.setattr(module.list_ports,'comports',lambda:[SimpleNamespace(device='COM5',vid=0x0483,pid=0x374b)])
    stm=client.get('/api/debug/tool-status',params={'target':'stm32h723'}).json()
    assert stm['gdb']==['C:/gdb.exe'] and stm['openocd']==[] and stm['utility']==['C:/programmer.exe']
    assert stm['development']=={'compiler':['C:/gcc.exe'],'vscode':[]}
    assert stm['device']=={'status':'serial_seen','ports':['COM5']}
    esp=client.get('/api/debug/tool-status',params={'target':'esp32s3'}).json()
    assert esp['gdb']==[] and esp['device']=={'status':'not_observed','ports':[]}
    assert client.get('/api/debug/tool-status',params={'target':'invalid'}).status_code==422


def test_hardware_debugger_mi_transport_with_simulated_subprocess(client,tmp_path):
    from pygdbmi.IoManager import IoManager
    ident=create_project(client);gdb=tmp_path/'arm-none-eabi-gdb.exe';gdb.write_bytes(b'fake')
    elf=tmp_path/'firmware.elf';elf.write_bytes(b'\x7fELF'+bytes([1,1,1])+bytes(11)+(40).to_bytes(2,'little'))
    script=tmp_path/'fake_mi.py'
    script.write_text('''import sys,re\nfor line in sys.stdin:\n m=re.match(r"(\\d+)(.*)",line.strip())\n if not m: continue\n token,cmd=m.groups()\n if cmd.startswith("-target-select"):\n  print(token+"^connected",flush=True);print('*stopped,reason="signal-received",frame={func="main",line="12"}',flush=True)\n elif cmd.startswith("-data-evaluate-expression"):\n  print(token+'^done,value="42"',flush=True)\n else: print(token+"^done",flush=True)\n''',encoding='utf8')
    def factory(_):
        proc=subprocess.Popen([sys.executable,'-u',str(script)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
        return proc,IoManager(proc.stdin,proc.stdout,proc.stderr,time_to_check_for_additional_output_sec=.05)
    client.app.state.debugger.factory=factory
    result=client.post(f'/api/projects/{ident}/debug/connect',json={'target':'stm32h723','gdb_path':str(gdb),'elf_path':str(elf),'port':61234})
    assert result.status_code==200
    deadline=time.monotonic()+2;state=result.json()
    while state['run_state']!='stopped' and time.monotonic()<deadline:
        state=client.get(f'/api/projects/{ident}/debug').json();time.sleep(.02)
    assert state['run_state']=='stopped'
    assert client.post(f'/api/projects/{ident}/debug/variables',json={'names':['counter']}).json()[0]['value']=='42'
    assert client.post(f'/api/projects/{ident}/debug/disconnect').status_code==200


def test_debug_timeout_remains_unknown_even_if_late_notification_arrives():
    from hardware_debug import HardwareDebugger,DebugError
    class Process:
        stdin=stdout=stderr=None
        def poll(self):return None
        def terminate(self):pass
        def wait(self,timeout):return 0
    class IO:
        def write(self,*args,**kwargs):return []
        def get_gdb_response(self,**kwargs):return [{'type':'notify','message':'stopped','payload':{}}]
    d=HardwareDebugger();d.process=Process();d.io=IO();d.state.update(connected=True,project_id=1,run_state='stopped')
    with pytest.raises(DebugError):d._request('-exec-next',timeout=0)
    assert d.snapshot()['run_state']=='unknown'
    with pytest.raises(DebugError):d.action(1,'continue')
    d.close()


def test_debug_source_rejects_network_paths(monkeypatch):
    from hardware_debug import HardwareDebugger,DebugError
    d=HardwareDebugger();d.state.update(connected=True,project_id=1,run_state='stopped',frame={'fullname':r'\\server\share\main.c','line':'1'})
    monkeypatch.setattr(d,'_check',lambda _:None)
    with pytest.raises(DebugError,match='本机源码路径'):d.source(1)


def test_serial_custom_format_and_incomplete_line(monkeypatch,client):
    ident=create_project(client);received={}
    class FakePort:
        def __init__(self):self.chunks=[b'{"v":2}\npartial']
        def read(self,n):
            if self.chunks:return self.chunks.pop(0)
            time.sleep(.01);return b''
        def cancel_read(self):pass
        def close(self):pass
    fake=FakePort()
    def open_serial(*args,**kwargs):received.update(kwargs);return fake
    monkeypatch.setattr(module.list_ports,'comports',lambda:[SimpleNamespace(device='COM99',description='Fake')])
    monkeypatch.setattr(module.serial,'Serial',open_serial)
    result=client.post('/api/serial/start',json={'project_id':ident,'port':'COM99','baud':19200,'bytesize':7,'parity':'E','stopbits':2})
    assert result.status_code==200 and result.json()['parity']=='E'
    assert (received['baudrate'],received['bytesize'],received['parity'],received['stopbits'])==(19200,7,'E',2)
    deadline=time.monotonic()+2
    while client.get('/api/serial').json()['received']<1 and time.monotonic()<deadline:time.sleep(.01)
    result=client.post('/api/serial/stop').json()
    assert result['invalid']==1
    logs=client.get(f'/api/projects/{ident}/serial/logs').json()
    assert '未完成的行' in logs[-1]['text'] and 'partial' in logs[-1]['text']
    assert client.post('/api/serial/start',json={'project_id':ident,'port':'COM99','baud':19200,'parity':'bad'}).status_code==422


def test_resources_upload_download_and_path_not_exposed(client,tmp_path):
    a=create_project(client);b=create_project(client,'项目二')
    response=client.post(f'/api/projects/{a}/resources/upload',params={'name':'../测试.txt'},content=b'hello')
    assert response.status_code==201
    ident=response.json()['id'];assert client.get(f'/api/projects/{a}/resources/{ident}/download').content==b'hello'
    assert client.get(f'/api/projects/{b}/resources/{ident}/download').status_code==404
    r=client.post(f'/api/projects/{a}/resources/path',json={'name':'原理图','path':r'C:\\docs\\test.pdf'})
    assert r.status_code==201 and client.get(f'/api/projects/{a}/resources/{r.json()["id"]}/download').status_code==404
    assert client.post(f'/api/projects/{a}/resources/upload',params={'name':'empty.txt'},content=b'').status_code==400
    assert not (tmp_path/'测试.txt').exists()


def test_cross_origin_and_invalid_samples(client):
    a=create_project(client)
    assert client.post(f'/api/projects/{a}/samples',json={'values':{} }).status_code==422
    assert client.post('/api/projects',headers={'Origin':'https://example.com'},json={'name':'x'}).status_code==403
    assert client.get('/api/projects',headers={'Host':'malicious.example'}).status_code==403
    assert client.post('/api/projects',json={'name':' ', 'unexpected':1}).status_code==422


def test_concurrent_data_do_not_duplicate_active_alert(client):
    a=create_project(client);client.post(f'/api/projects/{a}/rules',json={'channel':'v','maximum':10})
    with ThreadPoolExecutor(max_workers=5) as pool:result=list(pool.map(lambda n:client.post(f'/api/projects/{a}/samples',json={'values':{'v':n}}),range(11,16)))
    assert all(x.status_code==201 for x in result)
    assert len(client.get(f'/api/projects/{a}/samples').json())==5
    assert len(client.get(f'/api/projects/{a}/alerts').json())==1


def test_csv_export_is_complete_scoped_and_safe_for_spreadsheets(client,tmp_path):
    a=create_project(client);b=create_project(client,'另一个项目')
    with sqlite3.connect(tmp_path/'test.sqlite3') as c:
        c.executemany('INSERT INTO samples(project_id,source,values_json,created_at) VALUES (?,?,?,?)',
          [(a,'serial',json.dumps({'temperature':i,'=1+2':3}),f'2026-01-01T00:00:{i%60:02}.000+00:00') for i in range(305)])
        c.execute('INSERT INTO samples(project_id,source,values_json,created_at) VALUES (?,?,?,?)',(b,'manual',json.dumps({'other':5}),'2026-01-01T00:00:00Z'))
    assert len(client.get(f'/api/projects/{a}/samples').json())==300
    response=client.get(f'/api/projects/{a}/samples/export')
    assert response.status_code==200 and 'attachment' in response.headers['content-disposition']
    rows=list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert len(rows)==610 and rows[0]['channel']=='temperature' and rows[1]['channel']=="'=1+2"
    assert all(row['source']=='serial' and row['channel']!='other' for row in rows)
    assert client.get('/api/projects/999/samples/export').status_code==404


def test_test_records_report_isolation_and_validation(client):
    a=create_project(client);b=create_project(client,'其他项目')
    test={'title':'串口回环','setup':'115200','expected':'收到正确数据','actual':'结果一致','result':'通过'}
    assert client.post(f'/api/projects/{a}/tests',json=test).status_code==201
    assert client.post(f'/api/projects/{a}/tests',json={**test,'actual':''}).status_code==422
    assert client.post(f'/api/projects/{a}/tests',json={**test,'result':'未执行','actual':''}).status_code==201
    assert client.post(f'/api/projects/{b}/tests',json={**test,'title':'不应出现在其他项目'}).status_code==201
    client.post(f'/api/projects/{a}/notes',json={'category':'故障记录','title':'串口乱码','content':'确认波特率'})
    response=client.get(f'/api/projects/{a}/report')
    assert response.status_code==200 and 'attachment' in response.headers['content-disposition']
    assert '串口回环' in response.text and '确认波特率' in response.text and '实际：结果一致' in response.text
    assert '不应出现在其他项目' not in response.text
    assert client.get('/api/projects/999/report').status_code==404


def test_backup_contains_consistent_database_and_uploaded_files(client,tmp_path):
    ident=create_project(client)
    client.post(f'/api/projects/{ident}/notes',json={'category':'调试日志','title':'复位测试','content':'观察复位引脚'})
    result=client.post(f'/api/projects/{ident}/resources/upload',params={'name':'电路.txt'},content=b'board-data')
    assert result.status_code==201
    client.post(f'/api/projects/{ident}/resources/path',json={'name':'外部资料','path':r'C:\elsewhere\design.pdf'})
    response=client.get('/api/backup')
    assert response.status_code==200 and 'attachment' in response.headers['content-disposition']
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        names=z.namelist();assert 'board-debug.sqlite3' in names and 'backup-info.json' in names
        meta=json.loads(z.read('backup-info.json'));assert meta['uploaded_files']==1 and meta['local_path_references']==1
        upload_names=[x for x in names if x.startswith('uploads/')];assert len(upload_names)==1 and z.read(upload_names[0])==b'board-data'
        copy=tmp_path/'restored-copy.sqlite3';copy.write_bytes(z.read('board-debug.sqlite3'))
    with sqlite3.connect(copy) as c:
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert c.execute('SELECT content FROM notes WHERE project_id=?',(ident,)).fetchone()[0]=='观察复位引脚'
    for f in (tmp_path/'uploads').iterdir():f.unlink()
    assert client.get('/api/backup').status_code==409


def test_configured_storage_survives_application_restart(tmp_path,monkeypatch):
    db=tmp_path/'data'/'workbench.sqlite3'; uploads=tmp_path/'data'/'uploads'
    monkeypatch.setenv('BOARD_DEBUG_DB',str(db))
    monkeypatch.setenv('BOARD_DEBUG_UPLOADS',str(uploads))
    with TestClient(module.create_app(),base_url='http://127.0.0.1') as first:
        ident=create_project(first)
        upload=first.post(f'/api/projects/{ident}/resources/upload',params={'name':'记录.txt'},content=b'persisted')
        assert upload.status_code==201
        resource_id=upload.json()['id']
    with TestClient(module.create_app(),base_url='http://127.0.0.1') as second:
        assert any(p['id']==ident for p in second.get('/api/projects').json())
        assert second.get(f'/api/projects/{ident}/resources/{resource_id}/download').content==b'persisted'
