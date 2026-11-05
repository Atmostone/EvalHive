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
