"""从单一 BAT 启动隐藏服务，检查真实 HTTP 就绪；错误保留在日志及控制台。"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
if (ROOT / 'runtime/manifest.json').is_file():
    for _name in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'):
        os.environ.pop(_name, None)
    os.environ['PYTHONNOUSERSITE'] = '1'
ERROR_FILE=ROOT/'data/logs/startup-error.txt'
STATE_FILE=ROOT/'data/run/instance.json'


def current_version():
    """读取磁盘上的版本，避免复用升级前启动的旧后台。"""
    try:
        text=(ROOT/'backend/app.py').read_text(encoding='utf-8')
        match=re.search(r'^VERSION\s*=\s*["\']([^"\']+)',text,re.M)
        return match.group(1) if match else None
    except OSError:
        return None


def verify_ready(info):
    url=info['url']
    if not re.fullmatch(r'http://127\.0\.0\.1:\d+/',url):
        return False
    with urlopen(url,timeout=2) as response:
        html=response.read().decode('utf-8')
    token=re.search(r'name="subtitle-session" content="([^"]+)"',html)
    if not token:return False
    request=Request(url+'api/health',headers={'X-SubtitleStudio-Token':token.group(1)})
    with urlopen(request,timeout=2) as response:
        result=json.load(response)
    version=current_version()
    return (result.get('ok') is True
            and (version is None or result.get('version')==version)
            and Path(result['data_dir']).resolve()==(ROOT/'data').resolve())


def requested_port(arguments):
    for index,argument in enumerate(arguments):
        if argument=='--port' and index+1<len(arguments):
            try:return int(arguments[index+1])
            except ValueError:break
    return 8765


def _port_open(port, timeout=.05):
    with socket.socket() as probe:
        probe.settimeout(timeout)
        return probe.connect_ex(('127.0.0.1',port))==0


def find_running_instance(arguments):
    """已有本程序实例时直接复用，避免重复双击堆出多个后台服务。

    优先读实例状态文件；逐端口探测只作兜底，并且用很短超时——
    被防火墙丢包的端口每次都会等满超时，30 个端口能把启动拖慢近 10 秒。
    """
    start=requested_port(arguments)
    try:
        state=json.loads(STATE_FILE.read_text(encoding='utf-8'))
        url=state.get('url','')
        port=int(re.search(r':(\d+)/$',url).group(1))
    except (OSError,ValueError,TypeError,AttributeError):
        state=None;url='';port=-1
    if state is not None and ('--port' not in arguments or start<=port<start+30):
        try:
            if verify_ready({'url':url}):
                return url
        except Exception:
            pass
        try:STATE_FILE.unlink(missing_ok=True)
        except OSError:pass
    # 状态文件缺失（旧进程或已删除）时只探附近几个端口，够用且快。
    for port in range(start,start+6):
        if not _port_open(port):continue
        try:
            if verify_ready({'url':f'http://127.0.0.1:{port}/'}):
                return f'http://127.0.0.1:{port}/'
        except Exception:
            continue
    return None


def open_existing(url):
    subprocess.Popen([sys.executable,str(ROOT/'backend/app.py'),'--open-url',url],
                     cwd=ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)


def main():
    arguments=sys.argv[1:]
    ERROR_FILE.unlink(missing_ok=True)
    if '--console' in arguments:
        arguments.remove('--console')
        return subprocess.call([sys.executable,str(ROOT/'backend/app.py'),'--port','8765',*arguments],cwd=ROOT)
    print('正在启动字幕工作室…',flush=True)
    running=find_running_instance(arguments)
    if running:
        print('已在运行：'+running)
        if '--no-browser' not in arguments:
            try:open_existing(running)
            except OSError:pass
        return 0
    folder=ROOT/'data/logs';folder.mkdir(parents=True,exist_ok=True)
    interpreter=Path(sys.executable).with_name('pythonw.exe') if os.name=='nt' else Path(sys.executable)
    if not interpreter.exists():interpreter=Path(sys.executable)
    handle,name=tempfile.mkstemp(prefix='startup-',suffix='.json',dir=folder)
    os.close(handle);ready=Path(name)
    log_path=folder/'launcher.log'
    env={**os.environ,'PYTHONUTF8':'1','PYTHONUNBUFFERED':'1','SUBTITLESTUDIO_READY_FILE':str(ready)}
    process=None
    try:
        with log_path.open('ab') as output:
            process=subprocess.Popen([str(interpreter),str(ROOT/'backend/app.py'),'--port','8765',*arguments],
                cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=output,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if process.poll() is not None:
                raise RuntimeError(f'服务退出，返回码 {process.returncode}')
            try:
                info=json.loads(ready.read_text(encoding='utf-8'))
                if verify_ready(info):
                    print('启动成功：'+info['url'])
                    return 0
            except (OSError,ValueError,KeyError):
                pass
            time.sleep(.15)
        raise RuntimeError('等待服务就绪超时')
    except Exception as error:
        if process is not None and process.poll() is None:
            if os.name=='nt':
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True)
            else:process.terminate()
        diagnostic = 'run.bat --console' if (ROOT / 'run.bat').is_file() else 'py main.py'
        message=f'启动失败：{error}\n启动日志：{log_path}\n可运行 {diagnostic} 查看错误。'
        try:ERROR_FILE.write_text(message,encoding='utf-8')
        except OSError:pass
        print(message)
        return 1
    finally:
        ready.unlink(missing_ok=True)


if __name__=='__main__':raise SystemExit(main())
