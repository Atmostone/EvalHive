"""Benchmark fixture drives the host agent API without selecting a harness."""

import asyncio
import shlex
from copy import deepcopy

if __package__:
    from .broker_worker_fixture import PROVIDER, Worker, broker
else:
    from broker_worker_fixture import PROVIDER, Worker, broker
from evalhive_benchmark import ProtocolError, serve_stdio

# Local endpoint only. A native tool request proves that shell execution retains
# the task's Python dependencies and filesystem rather than a harness image's.
TOOL_SCRIPT = r"""
import os,time
from pathlib import Path
p=Path('/workspace/result.txt')
p.write_text(p.read_text()+'Driven through the Benchmark SDK\n')
if os.fork()==0:
 os.setsid()
 descriptor=os.open('/dev/null',os.O_RDWR)
 for target in (0,1,2): os.dup2(descriptor,target)
 Path('/workspace/agent-child.started').write_text('started')
 while True:
  Path('/workspace/agent-child.tick').write_text(str(time.monotonic()))
  time.sleep(.05)
"""
SERVER = r"""
import json,os,socket,time
from http.server import BaseHTTPRequestHandler,HTTPServer
if os.fork()==0:
 os.setsid()
 class Handler(BaseHTTPRequestHandler):
  count=0
  def log_message(self,*args): pass
  def do_POST(self):
   body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
   Handler.count+=1; first=Handler.count==1
   if first and not any(tool['function']['name']=='bash' for tool in body.get('tools',[])):
    self.send_error(400,'Expected native bash tool');return
   self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
   def send(value):
    data={'id':'fixture-'+str(Handler.count),'object':'chat.completion.chunk','created':1,'model':'fake-model',**value}
    self.wfile.write(('data: '+json.dumps(data)+'\n\n').encode());self.wfile.flush()
   command=TOOL_COMMAND_PLACEHOLDER
   delta={'role':'assistant','tool_calls':[{'index':0,'id':'native-shell','type':'function','function':{'name':'bash','arguments':json.dumps({'command':command,'description':'Verify native task dependencies'})}}]} if first else {'content':'Completed'}
   send({'choices':[{'index':0,'delta':delta,'finish_reason':None}]})
   send({'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls' if first else 'stop'}]})
   send({'choices':[],'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}})
   self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
 HTTPServer(('127.0.0.1',8765),Handler).serve_forever()
 raise SystemExit(0)
end=time.monotonic()+5
while time.monotonic()<end:
 try:
  with socket.create_connection(('127.0.0.1',8765),timeout=.2): break
 except OSError: time.sleep(.02)
else: raise RuntimeError('Local endpoint did not become ready')
print('Local endpoint ready')
""".replace("TOOL_COMMAND_PLACEHOLDER", repr("python -c " + shlex.quote(TOOL_SCRIPT)))


class AgentWorker(Worker):
    manifest = deepcopy(Worker.manifest)
    manifest.update(id="fixture.benchmark.agent", entrypoint=["python", "/fixture/agent_worker.py"])
    manifest["requirements"] = {"capabilities": ["host.agent_api_v1"]}

    async def handle(self, method, params, context):
        if method == "drive":
            result = await context.agent(
                params["scope"], "run", {}, request_id="agent.run", timeout=90
            )
            return {"agent_outcome": result["outcome"]}
        prepared = await super().handle(method, params, context)
        if method == "prepare":
            scope, handle = params["spec"]["scope"], prepared["session_handle"]
            try:
                await broker(
                    context,
                    scope,
                    PROVIDER,
                    "exec",
                    {
                        "argv": ["python", "-c", "raise RuntimeError('Private grant escaped')"],
                        "private_input_refs": {"FORBIDDEN": "agent.credentials"},
                    },
                    handle=handle,
                    request_id="probe.host.credentials",
                )
            except ProtocolError as error:
                if error.code != "PRIVATE_INPUT_UNAVAILABLE":
                    raise
            else:
                raise ProtocolError("FIXTURE_FAILED", "Worker received a host private grant")
            server = await broker(
                context,
                scope,
                PROVIDER,
                "exec",
                {
                    "argv": ["python", "-c", SERVER],
                    "timeout_seconds": 10,
                },
                handle=handle,
                request_id="prepare.local-endpoint",
            )
            if server["exit_code"] != 0:
                raise ProtocolError(
                    "FIXTURE_FAILED", "Local endpoint startup failed", "preparation"
                )
            prepared["agent"]["resources"] = [
                {"id": "tools", "kind": "tools", "config": {"names": ["bash"]}}
            ]
            prepared["agent"]["prompt"] = "Run the requested native shell check and finish."
        return prepared


if __name__ == "__main__":
    asyncio.run(serve_stdio(AgentWorker()))
