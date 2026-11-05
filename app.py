"""Local board-debug workspace; newline-delimited JSON over serial."""
from contextlib import asynccontextmanager, closing
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Literal
from urllib.parse import quote
import csv
import io
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
import serial
from serial.tools import list_ports
from hardware_debug import HardwareDebugger, DebugError

ROOT=Path(__file__).resolve().parent
MAX_LINE=16384
MAX_UPLOAD=25*1024*1024

def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds')
def connect(db):
    c=sqlite3.connect(db,timeout=5); c.row_factory=sqlite3.Row; c.execute('PRAGMA foreign_keys=ON');return c

class Strict(BaseModel): model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
class ProjectCreate(Strict):
    name:str=Field(min_length=1,max_length=100)
    board:str=Field(default='',max_length=120)
    description:str=Field(default='',max_length=1000)
class NoteCreate(Strict):
    category:Literal['调试日志','故障记录','测试结果']
    title:str=Field(min_length=1,max_length=120)
    content:str=Field(min_length=1,max_length=10000)
class RuleCreate(Strict):
    channel:str=Field(min_length=1,max_length=80)
    minimum:float|None=Field(default=None,allow_inf_nan=False)
    maximum:float|None=Field(default=None,allow_inf_nan=False)
    @model_validator(mode='after')
    def valid_range(self):
        if self.minimum is None and self.maximum is None: raise ValueError('至少填写一个阈值')
        if self.minimum is not None and self.maximum is not None and self.minimum>self.maximum: raise ValueError('下限不能大于上限')
        return self
class SerialStart(Strict):
    project_id:int=Field(gt=0)
    port:str=Field(min_length=1,max_length=120)
    baud:int=Field(ge=300,le=2000000)
    bytesize:Literal[7,8]=8
    parity:Literal['N','E','O']='N'
    stopbits:Literal[1,2]=1
class SerialSend(BaseModel):
    model_config=ConfigDict(extra='forbid')
    project_id:int=Field(gt=0)
    format:Literal['text','hex']='text'
    data:str=Field(min_length=1,max_length=4096)
    ending:Literal['none','lf','cr','crlf']='none'
    @model_validator(mode='after')
    def validate_payload(self):
        if self.format=='hex':
            cleaned=re.sub(r'\s+','',self.data)
            if not cleaned or len(cleaned)%2 or not re.fullmatch(r'[0-9a-fA-F]+',cleaned):raise ValueError('HEX 必须由完整的十六进制字节组成')
            if len(cleaned)>2048:raise ValueError('单次最多发送 1024 字节')
            if self.ending!='none':raise ValueError('HEX 模式不支持自动追加换行')
        elif len(self.data.encode('utf-8'))+len({'none':b'','lf':b'\n','cr':b'\r','crlf':b'\r\n'}[self.ending])>1024:
            raise ValueError('单次最多发送 1024 字节')
        return self
    def payload(self):
        if self.format=='hex':return bytes.fromhex(re.sub(r'\s+','',self.data))
        return self.data.encode('utf-8')+{'none':b'','lf':b'\n','cr':b'\r','crlf':b'\r\n'}[self.ending]
class ResourcePath(Strict):
    name:str=Field(min_length=1,max_length=200)
    path:str=Field(min_length=1,max_length=2000)
class DebugConnect(Strict):
    target:Literal['esp32s3','stm32h723']
    gdb_path:str=Field(min_length=1,max_length=2000)
    elf_path:str=Field(min_length=1,max_length=2000)
    port:int=Field(ge=1,le=65535)
class DebugAction(Strict):
    action:Literal['continue','pause','step_over','step_into']
class DebugBreakpoint(Strict):
    location:str=Field(min_length=1,max_length=300)
class DebugVariables(Strict):
    names:list[str]=Field(min_length=1,max_length=20)
class SampleCreate(Strict):
    values:dict[str,float]
    @model_validator(mode='after')
    def finite(self):
        if not self.values or len(self.values)>32 or any(not k or len(k)>80 or not math.isfinite(v) for k,v in self.values.items()): raise ValueError('请提供 1–32 个有限数值')
        return self
class TestRecordCreate(Strict):
    title:str=Field(min_length=1,max_length=120)
    setup:str=Field(default='',max_length=2000)
    expected:str=Field(min_length=1,max_length=2000)
    actual:str=Field(default='',max_length=2000)
    result:Literal['未执行','通过','失败','受阻']
    @model_validator(mode='after')
    def require_actual(self):
        if self.result in ('通过','失败') and not self.actual:raise ValueError('通过或失败时请填写实际结果')
        return self

def safe_csv_text(value):
    value=str(value)
    return "'"+value if value.startswith(('\t','\r','\n')) or value.lstrip().startswith(('=','+','-','@')) else value

def project_exists(c,pid):
    if not c.execute('SELECT id FROM projects WHERE id=?',(pid,)).fetchone(): raise HTTPException(404,'项目不存在')

def find_gdb_tools(target):
    """Inspect PATH and common local toolchain locations without launching software."""
    exe='xtensa-esp32s3-elf-gdb' if target=='esp32s3' else 'arm-none-eabi-gdb'
    names=[exe+'.exe',exe] if os.name=='nt' else [exe]
    found=[]
    for name in names:
        resolved=shutil.which(name)
        if resolved:found.append(Path(resolved))
    home=Path.home()
    if os.name=='nt':
        vendor='ESP32' if target=='esp32s3' else 'ST'
        installed=Path('D:/E_Lab_Base/01_Software_Installers')/vendor/'Debug_Tools'
        if installed.is_dir():
            for name in names:
                try:found.extend(path for path in installed.glob(f'*/bin/{name}') if path.is_file())
                except OSError:pass
    if target=='esp32s3':
        roots=[home/'.espressif'/'tools']
        if os.name=='nt':roots.insert(0,Path('D:/E_Lab_Base/01_Software_Installers/ESP32/idf-tools/tools'))
        patterns=[f'xtensa-esp-elf-gdb/*/*/bin/{name}' for name in names]+[f'xtensa-esp32s3-elf-gdb/*/*/bin/{name}' for name in names]
    else:
        roots=[Path(f'{drive}:/ST') for drive in ('C','D','F')] if os.name=='nt' else []
        patterns=[f'STM32CubeIDE*/STM32CubeIDE/plugins/*gnu-tools-for-stm32*/tools/bin/{name}' for name in names]
        for name in names:
            candidate=home/'.platformio'/'packages'/'toolchain-gccarmnoneeabi'/'bin'/name
            if candidate.is_file():found.append(candidate)
    for root in roots:
        if root.is_dir():
            for pattern in patterns:
                try:found.extend(path for path in root.glob(pattern) if path.is_file())
                except OSError:pass
    paths={str(x.resolve()) for x in found}
    return sorted(paths,key=lambda p:('idf-tools' not in p.lower(),p.lower()))[:30]


def find_related_tools(target):
    """Find known executables without launching them or touching connected boards."""
    root=Path('D:/E_Lab_Base/01_Software_Installers')
    if target=='esp32s3':
        items={
            'openocd':([root/'ESP32/Debug_Tools/openocd-esp32/bin/openocd.exe'],
                [(root/'ESP32/idf-tools/tools/openocd-esp32','*/openocd-esp32/bin/openocd.exe'),
                 (Path.home()/'.espressif/tools/openocd-esp32','*/openocd-esp32/bin/openocd.exe')]),
            'utility':([root/'ESP32/Debug_Tools/esptool-env/Scripts/esptool.exe'],[]),
        }
        command={'utility':'esptool'}
    else:
        items={
            'openocd':([] ,[(root/'ST/Debug_Tools','*/bin/openocd.exe')]),
            'utility':([root/'ST/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI.exe'],[]),
        }
        command={'openocd':'openocd','utility':'STM32_Programmer_CLI'}
    result={}
    for key,(paths,patterns) in items.items():
        if key in command:
            resolved=shutil.which(command[key])
            if resolved:paths.append(Path(resolved))
        for base,pattern in patterns:
            if base.is_dir():
                try:paths.extend(base.glob(pattern))
                except OSError:pass
        result[key]=sorted({str(p.resolve()) for p in paths if p.is_file()})[:30]
    return result


def find_development_tools(target):
    """Find common IDEs and build tools; a file's presence is not a build test."""
    root=Path('D:/E_Lab_Base/01_Software_Installers')
    idf_tools=root/'ESP32/idf-tools/tools'
    common={
        'vscode':([Path.home()/'AppData/Local/Programs/Microsoft VS Code/Code.exe',
                   Path('C:/Program Files/Microsoft VS Code/Code.exe')],[],['code']),
        'cmake':([] ,[(idf_tools/'cmake','*/bin/cmake.exe'),(Path.home()/'.espressif/tools/cmake','*/bin/cmake.exe')],['cmake']),
        'ninja':([] ,[(idf_tools/'ninja','*/ninja.exe'),(Path.home()/'.espressif/tools/ninja','*/ninja.exe')],['ninja']),
    }
    if target=='esp32s3':
        common.update({
            'compiler':([],[(idf_tools/'xtensa-esp-elf','*/xtensa-esp-elf/bin/xtensa-esp32s3-elf-gcc.exe'),
                            (Path.home()/'.espressif/tools/xtensa-esp-elf','*/xtensa-esp-elf/bin/xtensa-esp32s3-elf-gcc.exe')],['xtensa-esp32s3-elf-gcc']),
            'framework':([Path(os.environ['IDF_PATH'])/'tools/idf.py'] if os.environ.get('IDF_PATH') else [],
                         [(root/'ESP32','esp-idf-v*/tools/idf.py'),(Path.home()/'esp','esp-idf*/tools/idf.py')],['idf.py']),
        })
    else:
        common.update({
            'compiler':([],[(root/'ST/Debug_Tools','*/bin/arm-none-eabi-gcc.exe')],['arm-none-eabi-gcc']),
            'armclang':([root/'Keil/Keil_v5/ARM/ARMCLANG/bin/armclang.exe',root/'Keil/Keil_v5/ARM/ARMCC_506u7/bin/armcc.exe'],[],['armclang','armcc']),
            'keil':([root/'Keil/Keil_v5/UV4/UV4.exe',Path('C:/Keil_v5/UV4/UV4.exe')],[],['UV4']),
            'cubeide':([],[(Path('C:/ST'),'STM32CubeIDE*/STM32CubeIDE/stm32cubeide.exe'),
                           (root/'ST','STM32CubeIDE*/STM32CubeIDE/stm32cubeide.exe')],['stm32cubeide']),
        })
    result={}
    for key,(paths,patterns,commands) in common.items():
        for command in commands:
            resolved=shutil.which(command)
            if resolved:paths.append(Path(resolved))
        for base,pattern in patterns:
            if base.is_dir():
                try:paths.extend(base.glob(pattern))
                except OSError:pass
        result[key]=sorted({str(p.resolve()) for p in paths if p.is_file()})[:30]
    return result


def inspect_debug_tools(target):
    related=find_related_tools(target)
    try:
        ports=list(list_ports.comports())
        if target=='esp32s3':
            matches=[p.device for p in ports if getattr(p,'vid',None)==0x303a and getattr(p,'pid',None)==0x1001]
        else:
            matches=[p.device for p in ports if getattr(p,'vid',None)==0x0483 and getattr(p,'pid',None) in (0x374b,0x374e,0x374f)]
        device={'status':'serial_seen' if matches else 'not_observed','ports':matches}
    except (OSError,ValueError):
        device={'status':'unknown','ports':[]}
    return {'target':target,'gdb':find_gdb_tools(target),'openocd':related['openocd'],
            'utility':related['utility'],'device':device,'development':find_development_tools(target)}

def process_line(db,project_id,raw,source):
    """One complete UTF-8 JSON line. Returns False when it is not a numeric sample."""
    try:
        obj=json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(obj,dict) or not obj or len(obj)>64:return False
        values={k:float(v) for k,v in obj.items() if isinstance(k,str) and 0<len(k)<=80 and isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)}
        if not values or len(values)>32:return False
    except (ValueError,TypeError,OverflowError):return False
    with closing(connect(db)) as c,c:
        c.execute('BEGIN IMMEDIATE'); project_exists(c,project_id)
        timestamp=now();cur=c.execute('INSERT INTO samples(project_id,source,values_json,created_at) VALUES(?,?,?,?)',(project_id,source,json.dumps(values,ensure_ascii=False,allow_nan=False),timestamp))
        for rule in c.execute('SELECT * FROM rules WHERE project_id=?',(project_id,)).fetchall():
            if rule['channel'] not in values:continue
            value=values[rule['channel']];bad=(rule['minimum'] is not None and value<rule['minimum']) or (rule['maximum'] is not None and value>rule['maximum'])
            active=c.execute("SELECT id FROM alerts WHERE rule_id=? AND status='active'",(rule['id'],)).fetchone()
            if bad and not active:c.execute("INSERT INTO alerts(project_id,rule_id,sample_id,channel,value,status,created_at) VALUES (?,?,?,?,?,'active',?)",(project_id,rule['id'],cur.lastrowid,rule['channel'],value,timestamp))
            elif not bad and active:c.execute("UPDATE alerts SET status='recovered',recovered_at=? WHERE id=?",(timestamp,active['id']))
    return True

class SerialManager:
    def __init__(self,db):
        self.db=db;self.lock=Lock();self.tx_lock=Lock();self.port=None;self.stop_event=Event();self.thread=None;self.logs=deque(maxlen=200);self.state={'connected':False,'port':'','baud':None,'project_id':None,'error':'','received':0,'invalid':0}
    def snapshot(self):
        with self.lock:return dict(self.state)
    def start(self,pid,port,baud,bytesize=8,parity='N',stopbits=1):
        with self.lock:
            if self.thread and self.thread.is_alive(): raise HTTPException(409,'已有串口连接，请先断开')
            allowed={p.device for p in list_ports.comports()}
            if port not in allowed:raise HTTPException(400,'串口不在当前设备列表中，请刷新')
            try:self.port=serial.Serial(port,baudrate=baud,bytesize=bytesize,parity=parity,stopbits=stopbits,timeout=.25,write_timeout=2)
            except (serial.SerialException,ValueError,OSError) as e:raise HTTPException(400,'串口打开失败：'+str(e))
            self.stop_event=Event();self.logs.clear();self.state={'connected':True,'port':port,'baud':baud,'bytesize':bytesize,'parity':parity,'stopbits':stopbits,'project_id':pid,'error':'','received':0,'invalid':0}
            self.thread=Thread(target=self._read,args=(pid,self.port,self.stop_event),daemon=True);self.thread.start()
        return self.snapshot()
    def _read(self,pid,port,stop):
        buffer=bytearray();discarding=False
        try:
            while not stop.is_set():
                chunk=port.read(512)
                if not chunk:continue
                for byte in chunk:
                    if discarding:
                        if byte==10:discarding=False
                        continue
                    if byte==10:
                        raw=bytes(buffer);text=raw.decode('utf-8',errors='replace').strip()
                        try:
                            line=raw.decode('utf-8').strip();valid=process_line(self.db,pid,line,'serial')
                        except sqlite3.Error as e:
                            with self.lock:self.state['error']='数据保存失败，已停止采集：'+str(e)
                            stop.set();valid=False
                        except (UnicodeError,HTTPException):valid=False
                        buffer.clear()
                        with self.lock:
                            self.state['received' if valid else 'invalid']+=1
                            self.logs.append({'time':now(),'valid':valid,'text':text[:500],'truncated':len(text)>500})
                        if stop.is_set():break
                    else:
                        buffer.append(byte)
                        if len(buffer)>MAX_LINE:
                            buffer.clear();discarding=True
                            with self.lock:
                                self.state['invalid']+=1
                                self.logs.append({'time':now(),'valid':False,'text':'单行超过长度限制，已忽略','truncated':True})
        except (serial.SerialException,OSError) as e:
            with self.lock:self.state['error']='串口连接中断：'+str(e)
        finally:
            if buffer and not discarding:
                text=bytes(buffer).decode('utf-8',errors='replace').strip()
                with self.lock:
                    self.state['invalid']+=1
                    self.logs.append({'time':now(),'valid':False,'text':'未完成的行（已丢弃）：'+text[:450],'truncated':len(text)>450})
            try:port.close()
            except (serial.SerialException,OSError):pass
            with self.lock:self.state['connected']=False
    def stop(self):
        with self.tx_lock:
            with self.lock:thread=self.thread;self.stop_event.set();port=self.port
            if port:
                try:port.cancel_read()
                except (AttributeError,serial.SerialException,OSError):pass
            if thread:thread.join(timeout=2)
            with self.lock:
                if thread and thread.is_alive(): raise HTTPException(503,'串口仍在关闭，请稍后重试')
                self.state['connected']=False;self.port=None;self.thread=None
        return self.snapshot()
    def send(self,p:SerialSend):
        payload=p.payload()
        with self.tx_lock:
            with self.lock:
                if not self.state['connected'] or not self.port:raise HTTPException(409,'串口未连接')
                if self.state['project_id']!=p.project_id:raise HTTPException(409,'串口属于其他空间，请切回连接时的空间')
                port=self.port
            label=('HEX '+payload.hex(' ').upper()) if p.format=='hex' else ('文本 '+p.data+{'none':'','lf':' [LF]','cr':' [CR]','crlf':' [CRLF]'}[p.ending])
            try:
                written=port.write(payload)
            except (serial.SerialException,OSError) as e:
                with self.lock:self.logs.append({'time':now(),'direction':'tx','valid':None,'text':'发送失败（可能已部分发送）：'+label[:400],'truncated':len(label)>400})
                raise HTTPException(503,'发送失败，可能已部分发送，请检查设备状态：'+str(e))
            if written!=len(payload):
                with self.lock:self.logs.append({'time':now(),'direction':'tx','valid':None,'text':f'部分发送 {written}/{len(payload)} 字节：'+label[:400],'truncated':len(label)>400})
                raise HTTPException(503,f'仅发送 {written}/{len(payload)} 字节，请检查设备状态')
            with self.lock:self.logs.append({'time':now(),'direction':'tx','valid':None,'text':label[:500],'truncated':len(label)>500})
            return {'sent_bytes':written}
    def recent_logs(self,pid):
        with self.lock:return list(self.logs) if self.state['project_id']==pid else []

def create_app(database_path=None,upload_dir=None):
    db=Path(database_path or os.environ.get('BOARD_DEBUG_DB',ROOT/'board-debug.sqlite3'))
    uploads=Path(upload_dir or os.environ.get('BOARD_DEBUG_UPLOADS',ROOT/'uploads'))
    manager=SerialManager(db)
    debugger=HardwareDebugger()
    @asynccontextmanager
    async def lifespan(app):
        db.parent.mkdir(parents=True,exist_ok=True);uploads.mkdir(parents=True,exist_ok=True)
        with closing(connect(db)) as c,c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY,name TEXT NOT NULL,board TEXT NOT NULL,description TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),category TEXT NOT NULL,title TEXT NOT NULL,content TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rules(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),channel TEXT NOT NULL,minimum REAL,maximum REAL,UNIQUE(project_id,channel));
            CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),source TEXT NOT NULL,values_json TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS samples_project ON samples(project_id,id DESC);
            CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),rule_id INTEGER NOT NULL REFERENCES rules(id),sample_id INTEGER NOT NULL REFERENCES samples(id),channel TEXT NOT NULL,value REAL NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,recovered_at TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS active_rule_alert ON alerts(rule_id) WHERE status='active';
            CREATE TABLE IF NOT EXISTS resources(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),name TEXT NOT NULL,kind TEXT NOT NULL,path TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS test_records(id INTEGER PRIMARY KEY,project_id INTEGER NOT NULL REFERENCES projects(id),title TEXT NOT NULL,setup TEXT NOT NULL,expected TEXT NOT NULL,actual TEXT NOT NULL,result TEXT NOT NULL,created_at TEXT NOT NULL);
            ''')
            columns={row['name'] for row in c.execute('PRAGMA table_info(projects)')}
            if 'is_workspace' not in columns:
                c.execute('ALTER TABLE projects ADD COLUMN is_workspace INTEGER NOT NULL DEFAULT 0')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS single_independent_workspace ON projects(is_workspace) WHERE is_workspace=1')
            c.execute("INSERT OR IGNORE INTO projects(name,board,description,created_at,is_workspace) VALUES ('独立调试','','无需创建项目，可直接开始调试',?,1)",(now(),))
        yield
        manager.stop()
        debugger.close()
    app=FastAPI(title='开发板调试工作台',lifespan=lifespan)
    @app.exception_handler(DebugError)
    async def debug_error_handler(request,exc):return JSONResponse({'detail':str(exc)},status_code=409)
    app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
    @app.middleware('http')
    async def same_origin(request:Request,call_next):
        if request.url.hostname not in ('127.0.0.1','localhost','::1'):
            return JSONResponse({'detail':'仅允许本机访问'},status_code=403)
        origin=request.headers.get('origin')
        if origin and origin not in (f'http://{request.url.netloc}',f'https://{request.url.netloc}'):
            return JSONResponse({'detail':'请求来源不匹配'},status_code=403)
        return await call_next(request)
    @app.get('/',include_in_schema=False)
    def home():return FileResponse(ROOT/'static'/'index.html')
    @app.get('/api/projects')
    def projects():
        with closing(connect(db)) as c:return [dict(x) for x in c.execute('SELECT * FROM projects ORDER BY is_workspace DESC,id DESC')]
    @app.get('/api/backup')
    def backup():
        # Snapshot SQLite before collecting immutable uploads referenced by the snapshot.
        # Temporary files and the spooled archive are closed even if an error occurs.
        archive=tempfile.SpooledTemporaryFile(max_size=8*1024*1024,mode='w+b')
        try:
            with tempfile.TemporaryDirectory(prefix='boardlab-backup-') as tempdir:
                snapshot=Path(tempdir)/'board-debug.sqlite3'
                with closing(connect(db)) as source, closing(sqlite3.connect(snapshot)) as target:
                    source.backup(target)
                with closing(connect(snapshot)) as c:
                    rows=c.execute("SELECT DISTINCT path FROM resources WHERE kind='upload' ORDER BY path").fetchall()
                    project_count=c.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
                    path_count=c.execute("SELECT COUNT(*) FROM resources WHERE kind='path'").fetchone()[0]
                files=[]
                for row in rows:
                    name=row['path']
                    if not re.fullmatch(r'[0-9a-f]{32}',name):raise HTTPException(409,'发现无效的上传文件记录，无法完整备份')
                    file=uploads/name
                    if file.is_symlink() or not file.is_file() or file.resolve().parent!=uploads.resolve():
                        raise HTTPException(409,'有上传文件缺失或不可读取，无法完成备份')
                    files.append(file)
                with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,allowZip64=True) as z:
                    z.write(snapshot,'board-debug.sqlite3')
                    for file in files:z.write(file,'uploads/'+file.name)
                    z.writestr('backup-info.json',json.dumps({'format':'boardlab-backup','version':1,'created_utc':now(),'project_count':project_count,'uploaded_files':len(files),'local_path_references':path_count,'note':'本地路径仅包含引用，原路径上的文件不在备份中。'},ensure_ascii=False,indent=2))
            archive.seek(0)
        except BaseException:
            archive.close();raise
        def chunks():
            try:
                while block:=archive.read(1024*1024):yield block
            finally:archive.close()
        return StreamingResponse(chunks(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="boardlab-backup.zip"','X-Content-Type-Options':'nosniff'})
    @app.post('/api/projects',status_code=201)
    def add_project(p:ProjectCreate):
        with closing(connect(db)) as c,c:
            cur=c.execute('INSERT INTO projects(name,board,description,created_at) VALUES (?,?,?,?)',(p.name,p.board,p.description,now()))
            return dict(c.execute('SELECT * FROM projects WHERE id=?',(cur.lastrowid,)).fetchone())
    @app.put('/api/projects/{pid}')
    def update_project(pid:int,p:ProjectCreate):
        with closing(connect(db)) as c,c:
            project_exists(c,pid)
            row=c.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone()
            if row['is_workspace']:raise HTTPException(400,'独立调试空间不可重命名')
            c.execute('UPDATE projects SET name=?,board=?,description=? WHERE id=?',(p.name,p.board,p.description,pid))
            return dict(c.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone())
    @app.get('/api/projects/{pid}/notes')
    def notes(pid:int):
        with closing(connect(db)) as c:
            project_exists(c,pid);return [dict(x) for x in c.execute('SELECT * FROM notes WHERE project_id=? ORDER BY id DESC LIMIT 300',(pid,))]
    @app.post('/api/projects/{pid}/notes',status_code=201)
    def add_note(pid:int,p:NoteCreate):
        with closing(connect(db)) as c,c:
            project_exists(c,pid);cur=c.execute('INSERT INTO notes(project_id,category,title,content,created_at) VALUES (?,?,?,?,?)',(pid,p.category,p.title,p.content,now()))
            return dict(c.execute('SELECT * FROM notes WHERE id=?',(cur.lastrowid,)).fetchone())
    @app.get('/api/projects/{pid}/rules')
    def rules(pid:int):
        with closing(connect(db)) as c:project_exists(c,pid);return [dict(x) for x in c.execute('SELECT * FROM rules WHERE project_id=? ORDER BY id',(pid,))]
    @app.post('/api/projects/{pid}/rules',status_code=201)
    def add_rule(pid:int,p:RuleCreate):
        try:
            with closing(connect(db)) as c,c:
                project_exists(c,pid);cur=c.execute('INSERT INTO rules(project_id,channel,minimum,maximum) VALUES (?,?,?,?)',(pid,p.channel,p.minimum,p.maximum))
                return dict(c.execute('SELECT * FROM rules WHERE id=?',(cur.lastrowid,)).fetchone())
        except sqlite3.IntegrityError:raise HTTPException(409,'该通道已有规则')
    @app.put('/api/projects/{pid}/rules/{rid}')
    def update_rule(pid:int,rid:int,p:RuleCreate):
        try:
            with closing(connect(db)) as c,c:
                project_exists(c,pid)
                row=c.execute('SELECT * FROM rules WHERE id=? AND project_id=?',(rid,pid)).fetchone()
                if not row:raise HTTPException(404,'规则不存在')
                if row['channel']!=p.channel:raise HTTPException(400,'不能修改通道名')
                c.execute('UPDATE rules SET minimum=?,maximum=? WHERE id=?',(p.minimum,p.maximum,rid))
                return dict(c.execute('SELECT * FROM rules WHERE id=?',(rid,)).fetchone())
        except sqlite3.IntegrityError:raise HTTPException(409,'规则更新失败')
    @app.get('/api/projects/{pid}/samples')
    def samples(pid:int):
        with closing(connect(db)) as c:
            project_exists(c,pid);rows=c.execute('SELECT * FROM samples WHERE project_id=? ORDER BY id DESC LIMIT 300',(pid,)).fetchall()
            return [{**dict(x),'values':json.loads(x['values_json'])} for x in reversed(rows)]
    @app.post('/api/projects/{pid}/samples',status_code=201)
    def add_sample(pid:int,p:SampleCreate):
        with closing(connect(db)) as c:project_exists(c,pid)
        if not process_line(db,pid,json.dumps(p.values),'manual'):raise HTTPException(422,'无效数据')
        return {'ok':True,'source':'manual'}
    @app.get('/api/projects/{pid}/samples/export')
    def export_samples(pid:int):
        with closing(connect(db)) as c:
            project_exists(c,pid)
            ceiling=c.execute('SELECT COALESCE(MAX(id),0) FROM samples WHERE project_id=?',(pid,)).fetchone()[0]
        def generate():
            yield '\ufeff'
            buffer=io.StringIO(newline='');writer=csv.writer(buffer)
            writer.writerow(['sample_id','received_at_utc','source','channel','value'])
            yield buffer.getvalue();buffer.seek(0);buffer.truncate(0)
            after=0
            while after<ceiling:
                with closing(connect(db)) as c:
                    rows=c.execute('SELECT id,created_at,source,values_json FROM samples WHERE project_id=? AND id>? AND id<=? ORDER BY id LIMIT 500',(pid,after,ceiling)).fetchall()
                if not rows:break
                for row in rows:
                    for channel,value in json.loads(row['values_json']).items():
                        writer.writerow([row['id'],row['created_at'],row['source'],safe_csv_text(channel),value])
                after=rows[-1]['id'];yield buffer.getvalue();buffer.seek(0);buffer.truncate(0)
        return StreamingResponse(generate(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="board-samples-{pid}.csv"','X-Content-Type-Options':'nosniff'})
    @app.get('/api/projects/{pid}/tests')
    def tests(pid:int):
        with closing(connect(db)) as c:
            project_exists(c,pid);return [dict(x) for x in c.execute('SELECT * FROM test_records WHERE project_id=? ORDER BY id DESC LIMIT 300',(pid,))]
    @app.post('/api/projects/{pid}/tests',status_code=201)
    def add_test(pid:int,p:TestRecordCreate):
        with closing(connect(db)) as c,c:
            project_exists(c,pid)
            cur=c.execute('INSERT INTO test_records(project_id,title,setup,expected,actual,result,created_at) VALUES (?,?,?,?,?,?,?)',(pid,p.title,p.setup,p.expected,p.actual,p.result,now()))
            return dict(c.execute('SELECT * FROM test_records WHERE id=?',(cur.lastrowid,)).fetchone())
    @app.get('/api/projects/{pid}/report')
    def report(pid:int):
        with closing(connect(db)) as c:
            project_exists(c,pid);p=c.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone()
            records=[dict(x) for x in c.execute('SELECT * FROM test_records WHERE project_id=? ORDER BY id',(pid,))]
            entries=[dict(x) for x in c.execute('SELECT * FROM notes WHERE project_id=? ORDER BY id',(pid,))]
            warnings=[dict(x) for x in c.execute('SELECT * FROM alerts WHERE project_id=? ORDER BY id',(pid,))]
            total=c.execute('SELECT COUNT(*) FROM samples WHERE project_id=?',(pid,)).fetchone()[0]
        def label(s):return str(s).replace('\r',' ').replace('\n',' ')
        lines=['# 开发板调试记录报告','',f'生成时间（UTC）：{now()}',f'工作区：{label(p["name"])}',f'板卡：{label(p["board"] or "未填写")}',f'说明：{label(p["description"] or "未填写")}',f'保存的采样条数：{total}','', '数据来自已记录的串口或手动输入；时间为电脑接收时间。','', '## 测试记录','']
        if not records:lines.append('暂无测试记录。')
        for n,r in enumerate(records,1):
            lines += [f'### {n}. {label(r["title"])}',f'- 结果：{r["result"]}',f'- 时间（UTC）：{r["created_at"]}',f'- 条件：{label(r["setup"] or "未填写")}',f'- 预期：{label(r["expected"])}',f'- 实际：{label(r["actual"] or "未填写")}','']
        lines+=['','## 调试记录','']
        if not entries:lines.append('暂无调试记录。')
        for r in entries:lines.extend([f'### [{label(r["category"])}] {label(r["title"])}',f'时间（UTC）：{r["created_at"]}','',r['content'],''])
        lines+=['','## 阈值异常事件','']
        if not warnings:lines.append('暂无异常事件。')
        for r in warnings:lines.append(f'- {r["created_at"]}（UTC） | {label(r["channel"])} = {r["value"]} | '+('异常中' if r['status']=='active' else '已恢复')+(f' | 恢复时间：{r["recovered_at"]}' if r['recovered_at'] else ''))
        return Response('\n'.join(lines)+'\n',media_type='text/markdown; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="board-report-{pid}.md"','X-Content-Type-Options':'nosniff'})
    @app.get('/api/projects/{pid}/alerts')
    def alerts(pid:int):
        with closing(connect(db)) as c:project_exists(c,pid);return [dict(x) for x in c.execute('SELECT * FROM alerts WHERE project_id=? ORDER BY id DESC LIMIT 100',(pid,))]
    @app.get('/api/ports')
    def ports():return [{'port':x.device,'description':x.description} for x in list_ports.comports()]
    @app.get('/api/debug/tools')
    def debug_tools(target:Literal['esp32s3','stm32h723']):return {'paths':find_gdb_tools(target)}
    @app.get('/api/debug/tool-status')
    def debug_tool_status(target:Literal['esp32s3','stm32h723']):return inspect_debug_tools(target)
    @app.get('/api/serial')
    def serial_state():return manager.snapshot()
    @app.get('/api/projects/{pid}/serial/logs')
    def serial_logs(pid:int):
        with closing(connect(db)) as c:project_exists(c,pid)
        return manager.recent_logs(pid)
    @app.post('/api/serial/start')
    def start_serial(p:SerialStart):
        with closing(connect(db)) as c:project_exists(c,p.project_id)
        return manager.start(p.project_id,p.port,p.baud,p.bytesize,p.parity,p.stopbits)
    @app.post('/api/serial/stop')
    def stop_serial():return manager.stop()
    @app.post('/api/serial/send')
    def send_serial(p:SerialSend):return manager.send(p)
    @app.get('/api/projects/{pid}/debug')
    def debug_status(pid:int):
        with closing(connect(db)) as c:project_exists(c,pid)
        state=debugger.snapshot()
        if state.get('project_id') not in (None,pid):
            return {'connected':state['connected'],'foreign':True,'run_state':state['run_state'],'target':state['target']}
        return {**state,'foreign':False}
    @app.post('/api/projects/{pid}/debug/connect')
    def debug_connect(pid:int,p:DebugConnect):
        with closing(connect(db)) as c:project_exists(c,pid)
        return debugger.connect(pid,p.target,p.gdb_path,p.elf_path,p.port)
    @app.post('/api/projects/{pid}/debug/action')
    def debug_action(pid:int,p:DebugAction):
        with closing(connect(db)) as c:project_exists(c,pid)
        return debugger.action(pid,p.action)
    @app.post('/api/projects/{pid}/debug/breakpoints')
    def debug_breakpoint(pid:int,p:DebugBreakpoint):
        with closing(connect(db)) as c:project_exists(c,pid)
        return debugger.add_breakpoint(pid,p.location)
    @app.get('/api/projects/{pid}/debug/breakpoints')
    def debug_breakpoints(pid:int):
        with closing(connect(db)) as c:project_exists(c,pid)
        return debugger.breakpoints(pid)
    @app.delete('/api/projects/{pid}/debug/breakpoints/{number}')
    def debug_delete_breakpoint(pid:int,number:int):
        with closing(connect(db)) as c:project_exists(c,pid)
        return debugger.delete_breakpoint(pid,number)
    @app.post('/api/projects/{pid}/debug/variables')
    def debug_variables(pid:int,p:DebugVariables):
        with closing(connect(db)) as c:project_exists(c,pid)
        return debugger.variables(pid,p.names)
    @app.get('/api/projects/{pid}/debug/registers')
    def debug_registers(pid:int):
        with closing(connect(db)) as c:project_exists(c,pid)
        return debugger.registers(pid)
    @app.get('/api/projects/{pid}/debug/source')
    def debug_source(pid:int):
        with closing(connect(db)) as c:project_exists(c,pid)
        return debugger.source(pid)
    @app.post('/api/projects/{pid}/debug/disconnect')
    def debug_disconnect(pid:int):
        with closing(connect(db)) as c:project_exists(c,pid)
        return debugger.close_for_project(pid)
    @app.get('/api/projects/{pid}/resources')
    def resources(pid:int):
        with closing(connect(db)) as c:project_exists(c,pid);return [dict(x) for x in c.execute('SELECT id,project_id,name,kind,path,created_at FROM resources WHERE project_id=? ORDER BY id DESC',(pid,))]
    @app.post('/api/projects/{pid}/resources/path',status_code=201)
    def add_path(pid:int,p:ResourcePath):
        with closing(connect(db)) as c,c:
            project_exists(c,pid);cur=c.execute("INSERT INTO resources(project_id,name,kind,path,created_at) VALUES (?,?,'path',?,?)",(pid,p.name,p.path,now()))
            return {'id':cur.lastrowid}
    @app.post('/api/projects/{pid}/resources/upload',status_code=201)
    async def upload(pid:int,request:Request,name:str):
        with closing(connect(db)) as c:project_exists(c,pid)
        # Never use the supplied name as a filesystem path.
        name=Path(name.replace('\\','/')).name.strip()
        if not name or len(name)>200 or name in ('.','..'):raise HTTPException(400,'文件名无效')
        filename=uuid.uuid4().hex;destination=uploads/filename;length=0
        try:
            with destination.open('xb') as f:
                async for chunk in request.stream():
                    length+=len(chunk)
                    if length>MAX_UPLOAD:raise HTTPException(413,'文件不能超过 25 MB')
                    f.write(chunk)
            if not length:raise HTTPException(400,'文件为空')
            with closing(connect(db)) as c,c:
                project_exists(c,pid);cur=c.execute("INSERT INTO resources(project_id,name,kind,path,created_at) VALUES (?,?,'upload',?,?)",(pid,name,filename,now()))
            return {'id':cur.lastrowid,'name':name,'size':length}
        except BaseException:
            destination.unlink(missing_ok=True);raise
    @app.get('/api/projects/{pid}/resources/{rid}/download')
    def download(pid:int,rid:int):
        with closing(connect(db)) as c:
            project_exists(c,pid);r=c.execute("SELECT * FROM resources WHERE id=? AND project_id=? AND kind='upload'",(rid,pid)).fetchone()
        if not r:raise HTTPException(404,'文件不存在')
        target=uploads/r['path']
        if not target.is_file():raise HTTPException(404,'文件不存在')
        return FileResponse(target,filename=r['name'],media_type='application/octet-stream',headers={'X-Content-Type-Options':'nosniff'})
    app.state.serial_manager=manager
    app.state.debugger=debugger
    return app

app=create_app()
