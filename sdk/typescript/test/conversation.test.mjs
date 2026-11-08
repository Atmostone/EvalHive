import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {ConnectorClient, ProtocolError, validate, validateRun, serve, command, encodeLine, decodeLine} from '../dist/index.js';

const fixture = JSON.parse(readFileSync(new URL('../../fixtures/conversation.json',import.meta.url),'utf8'));
for(const example of fixture.cases)test(example.name,()=>{
  if(example.error)assert.throws(()=>validate(example.kind,example.value),error=>error.code===example.error);
  else assert.deepEqual(validate(example.kind,example.value),example.value);
});
test('interaction protocol and capabilities are explicit',()=>{
  validateRun(fixture.manifest,fixture.spec);
  const spec=structuredClone(fixture.spec);spec.interaction.version='2.0';
  assert.throws(()=>validateRun(fixture.manifest,spec),error=>error.code==='UNSUPPORTED_INTERACTION_VERSION');
  spec.interaction.version='1.0';const manifest=structuredClone(fixture.manifest);delete manifest.capabilities.external_tool_execution;
  assert.throws(()=>validateRun(manifest,spec),error=>error.code==='UNSUPPORTED_CAPABILITY');
});
for(const cancelled of [false,true])test(`success requires an explicit conversation close; cancelled=${cancelled}`,async()=>{
  const sent=[];let finish;
  const finished=new Promise(resolve=>{finish=resolve;});
  async function* input(){yield encodeLine(command('start','start',{run:fixture.spec}));await finished;yield encodeLine(command('shutdown','shutdown'));}
  await serve({manifest:fixture.manifest,run:async context=>{if(cancelled)context.abort.abort();return {status:'success',summary:'Premature'};}},input(),async frame=>{
    const event=decodeLine(frame);sent.push(event);if(event.event==='run.finished')finish();
  });
  const terminal=sent.find(event=>event.event==='run.finished');
  assert.equal(terminal.data.status,cancelled?'cancelled':'failed');
  if(!cancelled)assert.equal(terminal.data.error.code,'CONVERSATION_NOT_CLOSED');
});

for(const language of ['python','typescript']){
  const argv=language==='python'?[process.env.EH_TEST_PYTHON??'python3',fileURLToPath(new URL('../../examples/fake/connector.py',import.meta.url))]:
    [process.execPath,fileURLToPath(new URL('../../examples/fake/connector.mjs',import.meta.url))];
  const open=async(spec=fixture.spec)=>{
    const client=ConnectorClient.launch(argv),events=[],queue=[];let wake;
    const collecting=(async()=>{for await(const event of client.events()){events.push(event);queue.push(event);wake?.();}})();
    const next=async kind=>{
      const timeout=AbortSignal.timeout(5000);
      while(true){
        if(queue.length){const event=queue.shift();if(event.event===kind)return event;continue;}
        await new Promise((resolve,reject)=>{wake=resolve;timeout.addEventListener('abort',()=>reject(Error('Event timeout')),{once:true});});
      }
    };
    await client.request('start',{run:spec});await next('session.waiting_input');
    return{client,events,next,control:(command,payload)=>client.request('control',{attempt_id:fixture.spec.attempt_id,command,payload}),
      close:async()=>{await client.close();await collecting;}};
  };
  test(`persistent turns and replayed input: ${language}`,async()=>{
    const session=await open();
    try{
      const receipt=await session.control('turn.submit',fixture.turn);await session.next('turn.completed');
      assert.ok(!session.events.some(event=>event.event==='run.finished'));
      assert.deepEqual(await session.control('turn.submit',fixture.turn),receipt);
      await assert.rejects(session.control('turn.submit',{...fixture.turn,messages:[{role:'user',content:'changed'}]}),error=>error.code==='INPUT_CONFLICT');
      await session.control('turn.submit',{input_id:'input-2',turn_id:'turn-2',expected_revision:1,messages:[{role:'user',content:'second'}]});
      await session.next('turn.completed');
      await session.control('session.close',{input_id:'close',expected_revision:2,reason:'user_stop'});
      const end=await session.next('run.finished');assert.equal(end.data.status,'success');assert.equal(end.data.summary,'Echo: second');
      assert.equal(session.events.filter(event=>event.event==='turn.started').length,2);
    }finally{await session.close();}
  });
  test(`idle close after the input limit: ${language}`,async()=>{
    const spec=structuredClone(fixture.spec);spec.interaction.max_inputs=1;
    const session=await open(spec);
    try{
      await session.control('turn.submit',fixture.turn);await session.next('turn.completed');
      await session.control('session.close',{input_id:'close',expected_revision:1,reason:'finished'});
      assert.equal((await session.next('run.finished')).data.status,'success');
    }finally{await session.close();}
  });
  test(`external tools and abort: ${language}`,async()=>{
    const session=await open();
    try{
      await session.control('turn.submit',{...fixture.turn,messages:[{role:'user',content:'tools'}]});
      const request=await session.next('tool.request');assert.equal(request.data.calls.length,2);
      await session.control('tool.respond',{input_id:'second-result',turn_id:'turn-1',batch_id:'batch-turn-1',expected_revision:1,
        results:[{call_id:'second',content:'two',is_error:false}]});
      await session.control('abort',{});
      assert.equal((await session.next('run.finished')).data.status,'cancelled');
    }finally{await session.close();}
  });
}
