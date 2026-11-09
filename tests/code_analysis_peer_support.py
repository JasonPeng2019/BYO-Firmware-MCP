"""Shared code analysis peer support fixtures; no collected cases."""

from __future__ import annotations
import json
import sys
from pathlib import Path
from pyocd_debug_mcp.adapters.clangd import ClangdAdapter
from pyocd_debug_mcp.kernel.processes import ProcessMarkerStore


PEER = r"""
import json, sys, threading, time
lock = threading.Lock()
mode = sys.argv[1]
def send(value):
    data = json.dumps(value, ensure_ascii=False).encode('utf-8')
    frame = b'Content-Length: ' + str(len(data)).encode() + b'\r\n\r\n' + data
    with lock:
        for offset in range(0, len(frame), 7):
            sys.stdout.buffer.write(frame[offset:offset+7])
            sys.stdout.buffer.flush()
def result(request, value):
    send({'jsonrpc':'2.0','id':request['id'],'result':value})
def diagnostics(params):
    doc = params['textDocument']
    if mode == 'silent': return
    value = {'uri':doc['uri'], 'diagnostics':[]}
    if mode != 'unversioned': value['version'] = doc['version']
    send({'jsonrpc':'2.0','method':'textDocument/publishDiagnostics','params':value})
while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line: sys.exit(0)
        if line == b'\r\n': break
        key, value = line.decode().split(':',1)
        headers[key.lower()] = value.strip()
    data = sys.stdin.buffer.read(int(headers['content-length']))
    msg = json.loads(data)
    with open(sys.argv[2], 'a', encoding='utf-8') as log:
        log.write(json.dumps(msg, ensure_ascii=False) + '\n')
    method = msg.get('method')
    if method == 'initialize':
        result(msg, {'capabilities':{'positionEncoding':'utf-8','textDocumentSync':{'openClose':True,'change':1},
            'definitionProvider':True,'referencesProvider':True,'hoverProvider':True}})
    elif method in ('textDocument/didOpen','textDocument/didChange'): diagnostics(msg['params'])
    elif method == 'shutdown': result(msg, None)
    elif method == 'exit': sys.exit(0)
    elif method == 'test/crash': sys.exit(9)
    elif method == 'test/malformed':
        sys.stdout.buffer.write(b'Content-Length: 1\r\n\r\nx'); sys.stdout.buffer.flush()
    elif method == 'test/serverRequest':
        send({'jsonrpc':'2.0','id':'edit','method':'workspace/applyEdit','params':{'edit':{}}})
        saved = msg
    elif method is None and msg.get('id') == 'edit': result(saved, msg['result'])
    elif method == 'test/delay':
        def delayed(request):
            time.sleep(request['params']['seconds'])
            result(request, request['params']['value'])
        threading.Thread(target=delayed,args=(msg,),daemon=True).start()
    elif method == 'test/wedge': threading.Event().wait()
    elif method and method.startswith('textDocument/') and 'id' in msg:
        if mode == 'never-definition' and method.endswith('definition'): continue
        if mode == 'delayed': time.sleep(.2)
        if method.endswith('hover'): result(msg, {'contents':{'kind':'markdown','value':'type café 😀'}})
        elif method.endswith('references'): result(msg, [])
        else: result(msg, {'uri':msg['params']['textDocument']['uri'],'range':{'start':{'line':0,'character':4},'end':{'line':0,'character':9}}})
"""


def make_project(root: Path) -> Path:
    source = root / "main.cpp"
    source.write_bytes(b"int value = 1;\r\n")
    (root / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "file": str(source),
                    "arguments": ["clang++", "-std=c++17", "-c", str(source)],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return source


def fake_factory(root: Path, mode: str = "normal"):
    peer = root / "peer.py"
    peer.write_text(PEER, encoding="utf-8")

    class PeerAdapter(ClangdAdapter):
        def _argv(self) -> list[str]:
            return [sys.executable, str(peer), mode, str(root / "received.jsonl")]

    def factory(program, snapshot, marker_store=None):
        return PeerAdapter(program, snapshot, marker_store or ProcessMarkerStore(root / "markers"))

    return factory
