"""Experimental local GDB/MI client. It never starts a server or flashes firmware."""
from collections import deque
from pathlib import Path
from threading import RLock
import os
import re
import subprocess
import time
from pygdbmi.IoManager import IoManager


class DebugError(Exception):
    pass


def quote_mi(value):
    if any(ord(c)<32 for c in value):raise DebugError('路径或表达式不能包含控制字符')
    return '"'+value.replace('\\','/').replace('"','\\"')+'"'


class HardwareDebugger:
    """A single local GDB subprocess. All operations are serialized by one lock."""
    def __init__(self,transport_factory=None):
        self.lock=RLock();self.process=None;self.io=None;self.token=0;self.stop_seq=0;self.uncertain=False;self.factory=transport_factory or self._spawn
        self.state={'connected':False,'target':'','project_id':None,'run_state':'disconnected','frame':None,'reason':'','error':'','stop_seq':0}
        self.events=deque(maxlen=120)

    @staticmethod
    def _spawn(executable):
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        proc=subprocess.Popen([str(executable),'--nx','--quiet','--interpreter=mi2','-iex','set auto-load off'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0,shell=False,creationflags=flags)
        try:
            return proc,IoManager(proc.stdin,proc.stdout,proc.stderr,time_to_check_for_additional_output_sec=.05)
        except BaseException:
            try:proc.terminate();proc.wait(timeout=2)
            except (OSError,subprocess.TimeoutExpired):
                try:proc.kill();proc.wait(timeout=1)
                except (OSError,subprocess.TimeoutExpired):pass
            for stream in (proc.stdin,proc.stdout,proc.stderr):
                if stream:stream.close()
            raise

    def _absorb(self,records):
        for r in records:
            if self.uncertain and r.get('type')=='notify' and r.get('message') in ('running','stopped'):
                continue
            if r.get('type')=='notify' and r.get('message')=='running':
                self.state.update(run_state='running',frame=None,reason='')
            elif r.get('type')=='notify' and r.get('message')=='stopped':
                self.stop_seq+=1;payload=r.get('payload') or {};self.state.update(run_state='stopped',frame=payload.get('frame'),reason=payload.get('reason',''),stop_seq=self.stop_seq)
            elif r.get('type') in ('console','log','target'):
                text=str(r.get('payload') or '')[:800]
                if text:self.events.append(text)

    def _request(self,command,timeout=5):
        if not self.io:raise DebugError('调试器未连接')
        self.token+=1;token=self.token
        try:
            records=self.io.write(str(token)+command,timeout_sec=min(timeout,.5),raise_error_on_timeout=False)
            deadline=time.monotonic()+timeout
            while True:
                self._absorb(records)
                for r in records:
                    if r.get('type')=='result' and r.get('token')==token:
                        if r.get('message')=='error':raise DebugError(str((r.get('payload') or {}).get('msg','GDB 命令失败')))
                        if r.get('message')=='running' and not any(x.get('type')=='notify' and x.get('message')=='stopped' for x in records):
                            self.state.update(run_state='running',frame=None,reason='')
                        return r.get('payload') or {}
                if time.monotonic()>=deadline:
                    self.uncertain=True
                    self.state.update(run_state='unknown',error='调试命令超时，目标状态不确定；请断开后检查')
                    raise DebugError(self.state['error'])
                records=self.io.get_gdb_response(timeout_sec=.2,raise_error_on_timeout=False)
        except DebugError:raise
        except (OSError,ValueError,RuntimeError) as e:
            self.uncertain=True;self.state.update(run_state='unknown',error='GDB 通信失败：'+str(e))
            raise DebugError(self.state['error'])

    def connect(self,project_id,target,gdb_path,elf_path,port):
        with self.lock:
            if self.process:raise DebugError('已有调试连接，请先断开')
            executable=Path(gdb_path);elf=Path(elf_path)
            expected={'esp32s3':('xtensa-esp32s3-elf-gdb.exe','xtensa-esp32s3-elf-gdb'),
                      'stm32h723':('arm-none-eabi-gdb.exe','arm-none-eabi-gdb')}
            if target not in expected:raise DebugError('不支持的目标型号')
            if not executable.is_absolute() or not executable.is_file() or executable.name.lower() not in expected[target]:
                raise DebugError('请填写与目标型号匹配的 GDB 程序完整路径')
            if not elf.is_absolute() or not elf.is_file() or elf.suffix.lower() not in ('.elf','.axf'):
                raise DebugError('请选择包含调试符号的 ELF 或 AXF 文件完整路径')
            try:
                with elf.open('rb') as f:
                    header=f.read(20)
                    if len(header)<20 or header[:4]!=b'\x7fELF' or header[4]!=1 or header[5] not in (1,2):
                        raise DebugError('文件不是有效的 32 位 ELF 格式')
                    machine=int.from_bytes(header[18:20],'little' if header[5]==1 else 'big')
                    if machine!={'stm32h723':40,'esp32s3':94}[target]:
                        raise DebugError('固件架构与目标芯片不匹配，请选择对应的 ELF / AXF')
            except OSError as e:raise DebugError('无法读取程序文件：'+str(e))
            if not isinstance(port,int) or not 1<=port<=65535:raise DebugError('GDB 服务端口无效')
            try:
                self.process,self.io=self.factory(executable)
                self.events.clear();self.stop_seq=0;self.uncertain=False;self.state={'connected':False,'target':target,'project_id':project_id,'run_state':'connecting','frame':None,'reason':'','error':'','stop_seq':0}
                self._request('-gdb-set mi-async on');self._request('-gdb-set pagination off')
                self._request('-file-exec-and-symbols '+quote_mi(elf.as_posix()),timeout=8)
                self._request(f'-target-select remote 127.0.0.1:{port}',timeout=12)
                self.state['connected']=True
                if self.state['run_state']=='connecting':
                    try:
                        threads=self._request('-thread-info').get('threads',[])
                        states=[t.get('state') for t in threads if isinstance(t,dict)] if isinstance(threads,list) else []
                        self.state['run_state']='stopped' if 'stopped' in states else ('running' if 'running' in states else 'unknown')
                        if self.state['run_state']=='stopped':self.stop_seq+=1;self.state['stop_seq']=self.stop_seq
                    except DebugError:self.state['run_state']='unknown'
                return self.snapshot()
            except (OSError,ValueError,RuntimeError,DebugError) as e:
                self.close()
                raise DebugError(str(e))

    def snapshot(self):
        with self.lock:
            if self.process and self.process.poll() is not None:
                self.close();self.state['error']='GDB 进程已退出'
            elif self.io:
                try:self._absorb(self.io.get_gdb_response(timeout_sec=0,raise_error_on_timeout=False))
                except (OSError,ValueError,RuntimeError) as e:self.uncertain=True;self.state.update(run_state='unknown',error='读取 GDB 状态失败：'+str(e))
            return {**self.state,'events':list(self.events)}

    def action(self,project_id,action):
        commands={'continue':'-exec-continue','pause':'-exec-interrupt','step_over':'-exec-next','step_into':'-exec-step'}
        with self.lock:
            self._check(project_id)
            if action not in commands:raise DebugError('未知调试操作')
            if action in ('continue','step_over','step_into') and self.state['run_state']!='stopped':raise DebugError('目标未暂停，无法执行此操作')
            if action=='pause' and self.state['run_state']!='running':raise DebugError('目标未运行，无法暂停')
            self._request(commands[action]);return self.snapshot()

    def add_breakpoint(self,project_id,location):
        with self.lock:
            self._check(project_id)
            if not location or len(location)>300 or any(c in location for c in '\r\n\x00'):raise DebugError('断点位置无效')
            return self._request('-break-insert '+quote_mi(location))

    def breakpoints(self,project_id):
        with self.lock:
            self._check(project_id)
            table=self._request('-break-list').get('BreakpointTable',{})
            body=table.get('body',[]) if isinstance(table,dict) else []
            return [x.get('bkpt',x) for x in body if isinstance(x,dict)] if isinstance(body,list) else []

    def delete_breakpoint(self,project_id,number):
        with self.lock:
            self._check(project_id)
            if not isinstance(number,int) or number<1:raise DebugError('断点编号无效')
            return self._request(f'-break-delete {number}')

    def variables(self,project_id,names):
        with self.lock:
            self._check(project_id)
            if self.state['run_state']!='stopped':raise DebugError('请先暂停目标再读取变量')
            if not names or len(names)>20:raise DebugError('请输入 1–20 个变量名')
            result=[]
            for name in names:
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(?:(?:\.|->)[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*',name):
                    result.append({'name':name,'error':'仅支持变量、结构体字段和固定下标'});continue
                try:result.append({'name':name,'value':self._request('-data-evaluate-expression '+quote_mi(name)).get('value','')})
                except DebugError as e:
                    if self.state['run_state']=='unknown':raise
                    result.append({'name':name,'error':str(e)})
            return result

    def registers(self,project_id):
        with self.lock:
            self._check(project_id)
            if self.state['run_state']!='stopped':raise DebugError('请先暂停目标再读取寄存器')
            names=self._request('-data-list-register-names').get('register-names',[])
            values=self._request('-data-list-register-values x').get('register-values',[])
            return [{'name':names[int(x['number'])] if int(x['number'])<len(names) else x['number'],'value':x['value']}
                    for x in values if isinstance(x,dict) and str(x.get('number','')).isdigit() and 'value' in x]

    def source(self,project_id):
        with self.lock:
            self._check(project_id)
            if self.state['run_state']!='stopped':raise DebugError('请先暂停目标再读取源码')
            frame=self.state.get('frame') or {};name=frame.get('fullname');line=frame.get('line')
            if not name or not str(line or '').isdigit():raise DebugError('当前帧未提供源码路径和行号')
            path=Path(name)
            if str(name).startswith(('\\\\','//')) or not path.is_absolute() or path.suffix.lower() not in ('.c','.h','.cc','.cpp','.cxx','.hpp','.s','.asm'):
                raise DebugError('当前帧没有可读取的本机源码路径')
            try:
                path=path.resolve(strict=True)
                if str(path).startswith(('\\\\','//')):raise DebugError('不支持读取网络源码路径')
                with path.open('rb') as f:raw=f.read(2*1024*1024+1)
            except OSError as e:raise DebugError('无法读取源码文件：'+str(e))
            if len(raw)>2*1024*1024:raise DebugError('源码文件超过 2 MB，无法预览')
            try:text=raw.decode('utf-8-sig')
            except UnicodeDecodeError:text=raw.decode('gb18030',errors='replace')
            lines=text.splitlines();current=int(line)
            if current<1 or current>len(lines):raise DebugError('调试信息中的行号超出源码范围，请确认源码与固件匹配')
            first=max(1,current-12);last=min(len(lines),current+12)
            return {'path':str(path),'line':current,'stop_seq':self.state['stop_seq'],
                    'lines':[{'number':i,'text':lines[i-1],'current':i==current} for i in range(first,last+1)]}

    def _check(self,project_id):
        if not self.process or self.process.poll() is not None or not self.state['connected']:raise DebugError('调试器未连接')
        if self.state['project_id']!=project_id:raise DebugError('调试器属于其他空间，请切回连接时的空间')
        if self.uncertain or self.state['run_state']=='unknown':raise DebugError('目标状态不确定，请断开后检查')
        try:self._absorb(self.io.get_gdb_response(timeout_sec=0,raise_error_on_timeout=False))
        except (OSError,ValueError,RuntimeError) as e:
            self.uncertain=True;self.state.update(run_state='unknown',error='读取 GDB 状态失败：'+str(e));raise DebugError(self.state['error'])

    def close(self):
        with self.lock:
            proc=self.process;self.process=None;self.io=None
            if proc:
                try:
                    if proc.poll() is None:proc.terminate();proc.wait(timeout=2)
                except (OSError,subprocess.TimeoutExpired):
                    try:proc.kill();proc.wait(timeout=1)
                    except (OSError,subprocess.TimeoutExpired):pass
                for stream in (proc.stdin,proc.stdout,proc.stderr):
                    if stream:
                        try:stream.close()
                        except OSError:pass
            self.events.clear();self.uncertain=False;self.state.update(connected=False,target='',project_id=None,run_state='disconnected',frame=None,reason='',error='',stop_seq=0)
            return dict(self.state)

    def close_for_project(self,project_id):
        with self.lock:
            if self.process and self.state['project_id']!=project_id:raise DebugError('调试器属于其他空间，请切回连接时的空间')
            return self.close()
