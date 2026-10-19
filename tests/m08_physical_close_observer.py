"""Read-only physical observation after native core disconnect, before probe close.

Installed only for opt-in M08 physical proof by its external sitecustomize.
No production function is replaced and no target write is made. The observation
occurs in the existing worker at CortexM.disconnect's return, so comparator
absence is measured without a later connection or initialization sequence.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import traceback


def now():
    return datetime.now(timezone.utc).isoformat()


def observe_close(frame):
    configured = os.environ.get('M08T_PHYSICAL_CLOSE_CONFIG')
    if not configured:
        return
    config = json.loads(Path(configured).read_text(encoding='utf-8'))
    if not config.get('enabled'):
        return
    output = Path(config['output'])
    output.mkdir(parents=True, exist_ok=True)
    record = dict(started_utc=now(), pid=os.getpid(), phase=config['phase'],
                  stage='CortexM.disconnect return before native Session probe close',
                  resume_argument=frame.f_locals.get('resume'), target_writes=0,
                  initialization_or_reconnect=False, argv=sys.argv,
                  helper_path=__file__, helper_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    try:
        from pyocd_debug_mcp.kernel.processes import _start_token

        record['creation_time'] = _start_token(os.getpid())
        core = frame.f_locals['self']
        ap = core.ap
        control = int(ap.read_memory(0xE0002000, 32, now=True))
        count = (control >> 4) & 15 | (control >> 8) & 0x70
        assert 0 < count <= 127
        record.update(fp_ctrl=control,
                      comparators={str(0xE0002008 + 4*i): int(ap.read_memory(0xE0002008 + 4*i, 32, now=True))
                                   for i in range(count)},
                      dhcsr=int(ap.read_memory(0xE000EDF0, 32, now=True)),
                      dfsr=int(ap.read_memory(0xE000ED30, 32, now=True)),
                      pending_addresses=sorted(core.bp_manager._updated_breakpoints),
                      realized_addresses=sorted(core.bp_manager._breakpoints))
        images = []
        for segment in config['segments']:
            start, length = segment['start'], segment['length']
            data = bytes(ap.read_memory_block8(start, length))
            path = output/f"{config['phase']}-{os.getpid()}-{start:08x}.bin"
            path.write_bytes(data)
            images.append(dict(path=str(path), start=start, bytes=len(data),
                               sha256=hashlib.sha256(data).hexdigest(),
                               matches_expected=hashlib.sha256(data).hexdigest() == segment['sha256']))
        record['addressed_images'] = images
        record['observed'] = True
    except Exception:
        # Observation failure is evidence, never permission to alter teardown.
        record.update(observed=False, error=traceback.format_exc())
    finally:
        record['ended_utc'] = now()
        with (output/f"{os.getpid()}-close.jsonl").open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record) + '\n')


def install():
    previous = sys.getprofile()
    previous_thread = threading.getprofile()

    def profile(frame, event, arg):
        if previous is not None:
            previous(frame, event, arg)
        if (event == 'return' and frame.f_code.co_name == 'disconnect'
                and frame.f_globals.get('__name__') == 'pyocd.coresight.cortex_m'):
            observe_close(frame)

    def thread_profile(frame, event, arg):
        if previous_thread is not None:
            previous_thread(frame, event, arg)
        if (event == 'return' and frame.f_code.co_name == 'disconnect'
                and frame.f_globals.get('__name__') == 'pyocd.coresight.cortex_m'):
            observe_close(frame)

    sys.setprofile(profile)
    threading.setprofile(thread_profile)
