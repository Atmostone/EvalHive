import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
import {conversationFixture} from '../../shared/conversation.mjs';
import {stream} from '../../shared/fake-openai.mjs';

test('native server death while waiting for input fails without restarting the session', {timeout: 60000}, async () => {
  const entrypoint = fileURLToPath(new URL('../dist/index.js', import.meta.url));
  const fixture = await conversationFixture(entrypoint, (_body, index, response) => stream(response, index, {content: 'Idle reply'}));
  try {
    await fixture.start(); await fixture.submit(1, 'Generate');
    await fixture.wait('session.waiting_input', event => event.data.revision === 1);
    const event = fixture.events.find(item => item.event === 'native' && item.data.name === 'connector.stage' && item.data.payload.stage === 'server_ready');
    assert(Number.isInteger(event?.data.payload.pid));
    process.kill(event.data.payload.pid, 'SIGKILL');
    const terminal = await fixture.wait('run.finished');
    assert.equal(terminal.data.status, 'failed');
    assert.equal(terminal.data.error.code, 'SERVER_EXITED');
    assert.equal(fixture.endpoint.requests.length, 1);
    assert(!fixture.events.some(item => item.event === 'run.finished' && item.data.status === 'success'));
  } finally {await fixture.close();}
});
