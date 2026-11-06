import assert from 'node:assert/strict';
import test from 'node:test';
import {Peer, command, decodeLine, encodeLine} from '../dist/index.js';

const delay = milliseconds => new Promise(resolve => setTimeout(resolve,milliseconds));
test('replayed pending command executes once', async () => {
  const sent=[];let calls=0,release;
  const gate=new Promise(resolve=>{release=resolve;});
  const peer=new Peer(async frame=>{sent.push(decodeLine(frame));},{handler:async()=>{calls++;await gate;return {applied:calls};}});
  async function* input() {
    yield encodeLine(command('same-operation','describe'));
    yield encodeLine(command('same-operation','describe'));
    await delay(10);assert.equal(calls,1);release();await delay(10);
  }
  await peer.read(input());
  assert.deepEqual(sent.map(reply=>reply.result),[{applied:1},{applied:1}]);
});
test('receipt limit retains previous commands',async()=>{
  const sent=[];
  const peer=new Peer(async frame=>{sent.push(decodeLine(frame));},{receiptLimit:1,handler:async()=>({applied:true})});
  async function* input(){for(const id of ['first','second','first']){yield encodeLine(command(id,'describe'));await delay(10);}}
  await peer.read(input());
  assert.equal(sent[1].error.code,'LEDGER_FULL');assert.deepEqual(sent[2],sent[0]);
});
test('timeouts have an uncertain outcome',async()=>{
  const peer=new Peer(async()=>{});
  await assert.rejects(peer.request(command('request','describe'),10),error=>error.code==='COMMAND_UNCERTAIN');
});
test('acknowledged results become bounded non-reexecution tombstones',async()=>{
  const sent=[];let calls=0;
  const peer=new Peer(async frame=>{sent.push(decodeLine(frame));},{receiptLimit:1,handler:async()=>({applied:++calls})});
  async function* input(){
    yield encodeLine(command('first','describe'));await delay(10);
    const ack={protocol_version:'1.0',type:'receipt_ack',id:'first',request_type:'command'};
    yield encodeLine(ack);yield encodeLine(ack);
    yield encodeLine(command('second','describe'));await delay(10);
    yield encodeLine(command('first','describe'));await delay(10);
  }
  await peer.read(input());assert.equal(calls,2);assert.equal(sent[2].error.code,'RECEIPT_RETIRED');
});
test('unknown protocol and private exceptions are explicit and sanitized',async()=>{
  const sent=[];
  const peer=new Peer(async frame=>{sent.push(decodeLine(frame));},{handler:async()=>{throw new Error('private-example-value');}});
  async function* input(){yield encodeLine(command('first','describe'));await delay(10);
    yield Buffer.from(JSON.stringify({...command('second','describe'),protocol_version:'9.0'})+'\n');}
  await assert.rejects(peer.read(input()),error=>error.code==='UNSUPPORTED_PROTOCOL');
  assert.equal(sent[0].error.code,'CONNECTOR_ERROR');assert.equal(sent[1].error.code,'UNSUPPORTED_PROTOCOL');
  assert.ok(!JSON.stringify(sent).includes('private-example-value'));
});

test('durable host broker ACK bounds reply payloads across a long native loop', async()=>{
  const {PassThrough}=await import('node:stream');
  const {Context}=await import('../dist/index.js');
  const hostInput=new PassThrough(),workerInput=new PassThrough(),committed=new Map();let effects=0,lookups=0;
  const host=new Peer(async frame=>{workerInput.write(frame);},{requestType:'broker_request',receiptLimit:1,
    handler:async request=>{effects++;const result={handle:request.id};committed.set(request.id,result);return result;},
    replayHandler:async request=>{lookups++;return committed.get(request.id);}});
  const worker=new Peer(async frame=>{hostInput.write(frame);});
  const jobs=[host.read(hostInput),worker.read(workerInput)];
  try{
    const context=new Context(worker);
    for(let index=0;index<300;index++){
      const result=await context.broker({workspace_id:'workspace',trial_id:'trial',generation:1},'fixture','inspect',{},
        {requestId:`resource-${index}`,durableHost:true});
      assert.deepEqual(result,committed.get(`resource-${index}`));
    }
    assert.deepEqual(await context.broker({workspace_id:'workspace',trial_id:'trial',generation:1},'fixture','inspect',{},
      {requestId:'resource-299',durableHost:true}),committed.get('resource-299'));
  }finally{hostInput.end();workerInput.end();await Promise.all(jobs);}
  assert.equal(committed.size,300);assert.equal(effects,300);assert.equal(lookups,1);
});
